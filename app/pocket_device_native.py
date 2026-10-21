"""Narrow macOS USB Documents adapter for an isolated Stillroom worker.

This uses Apple's PRIVATE MobileDevice ABI, not a supported public SDK. Symbol
availability is checked before use; actual device/OS compatibility still needs a
physical test. The caller must run this module in a disposable subprocess with a
hard deadline: a private synchronous call is not safely cancellable in Python.
No function in this module pairs, installs, signs, launches, or changes settings.

Declaration references (consulted 2026-10-21):
- ios-control/ios-deploy src/ios-deploy/{MobileDevice.h,ios-deploy.m}
- NativeScript/ios-device-lib IOSDeviceLib/Declarations.h
Their old packed device structures are deliberately NOT used. AMDevice and CF
objects remain opaque pointers. The adapter is independently implemented here;
no upstream implementation is vendored. Windows direct transfer is unsupported.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
import hashlib
import plistlib
import re
import sys
from typing import Any

MAX_CHUNK = 256 * 1024
MAX_CF_BYTES = 1024 * 1024
MAX_DEVICES = 32
MAX_DIRECTORY_ENTRIES = 4096
APP_BUNDLE_IDS = ("com.nategrenade.stillroom.pocket.dev",)

# API references and handles are opaque. CFIndex/size_t follow the host ABI;
# AFCFileRef is 64-bit even though native status codes are 32-bit.
PTR = C.c_void_p
U32 = C.c_uint32
U64 = C.c_uint64
INDEX = C.c_long
SIZE = C.c_size_t
PPTR = C.POINTER(PTR)
UTF8 = 0x08000100


class NativeError(Exception):
    """Constant, non-sensitive error suitable for the worker's public JSON."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _invalid_path() -> NativeError:
    return NativeError("invalid_path", "The destination is outside the allowed app Documents area.")


def _relative(path: str, *, empty: bool = False) -> str:
    if not isinstance(path, str) or (not path and not empty):
        raise _invalid_path()
    if not path:
        return ""
    if len(path.encode("utf-8", errors="replace")) > 1024:
        raise _invalid_path()
    if path.startswith("/") or any(c in path for c in ("\\", "\x00", ":")):
        raise _invalid_path()
    parts = path.split("/")
    if len(parts) > 3 or any(p in ("", ".", "..") for p in parts):
        raise _invalid_path()
    for part in parts:
        try:
            encoded = part.encode("utf-8", errors="strict")
        except UnicodeError:
            raise _invalid_path() from None
        if len(encoded) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise _invalid_path()
    return path


def _safe_text(value: Any, fallback: str, limit: int = 120) -> str:
    if not isinstance(value, str):
        return fallback
    value = "".join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()
    return value[:limit] or fallback


def _afc_check(status: int) -> None:
    status = int(status)
    if status == 0:
        return
    if status == 8:
        raise NativeError("not_found", "The requested transfer item is no longer available.")
    if status == 16:
        raise NativeError("exists", "A destination item already exists; it was not replaced.")
    if status == 18:
        raise NativeError("no_space", "The iPhone reports insufficient space for this transfer.")
    raise NativeError("io_error", "The iPhone file-sharing operation did not complete.")


def _device_check(status: int, *, trust: bool = False) -> None:
    status = int(status) & 0xFFFFFFFF
    if status == 0:
        return
    if status == 0xE800000E:  # kAMDPasswordProtectedError
        raise NativeError("locked", "Unlock the iPhone, then try the cable connection again.")
    if trust:
        raise NativeError("trust_required", "Existing computer trust could not be validated. No new pairing was attempted.")
    raise NativeError("io_error", "The existing iPhone connection could not complete the requested operation.")


def _bind(library: Any, name: str, result: Any, args: list[Any]):
    try:
        fn = getattr(library, name)
    except AttributeError:
        raise NativeError("unavailable", "Required macOS device-support functions are unavailable.") from None
    fn.restype = result
    fn.argtypes = args
    return fn


class _CoreFoundation:
    def __init__(self):
        try:
            self.lib = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError:
            raise NativeError("unavailable", "The macOS CoreFoundation runtime could not be loaded.") from None
        self.release = _bind(self.lib, "CFRelease", None, [PTR])
        self.get_type = _bind(self.lib, "CFGetTypeID", C.c_ulong, [PTR])
        self.array_type = _bind(self.lib, "CFArrayGetTypeID", C.c_ulong, [])()
        self.dict_type = _bind(self.lib, "CFDictionaryGetTypeID", C.c_ulong, [])()
        self.string_type = _bind(self.lib, "CFStringGetTypeID", C.c_ulong, [])()
        self.array_count = _bind(self.lib, "CFArrayGetCount", INDEX, [PTR])
        self.array_value = _bind(self.lib, "CFArrayGetValueAtIndex", PTR, [PTR, INDEX])
        self.dict_value = _bind(self.lib, "CFDictionaryGetValue", PTR, [PTR, PTR])
        self.string_create = _bind(self.lib, "CFStringCreateWithCString", PTR, [PTR, C.c_char_p, U32])
        self.string_length = _bind(self.lib, "CFStringGetLength", INDEX, [PTR])
        self.string_max = _bind(self.lib, "CFStringGetMaximumSizeForEncoding", INDEX, [INDEX, U32])
        self.string_get = _bind(self.lib, "CFStringGetCString", C.c_bool, [PTR, PTR, INDEX, U32])
        self.data_create = _bind(self.lib, "CFDataCreate", PTR, [PTR, PTR, INDEX])
        self.data_length = _bind(self.lib, "CFDataGetLength", INDEX, [PTR])
        self.data_bytes = _bind(self.lib, "CFDataGetBytePtr", PTR, [PTR])
        self.plist_create = _bind(self.lib, "CFPropertyListCreateWithData", PTR, [PTR, PTR, C.c_ulong, PTR, PTR])
        self.plist_data = _bind(self.lib, "CFPropertyListCreateData", PTR, [PTR, PTR, INDEX, C.c_ulong, PTR])

    @contextmanager
    def text_ref(self, value: str):
        ref = self.string_create(None, value.encode("utf-8"), UTF8)
        if not ref:
            raise NativeError("io_error", "Device request metadata could not be prepared.")
        try:
            yield ref
        finally:
            self.release(ref)

    @contextmanager
    def plist_ref(self, value: dict):
        try:
            payload = plistlib.dumps(value, fmt=plistlib.FMT_XML)
        except (TypeError, ValueError, OverflowError):
            raise NativeError("io_error", "Device request metadata could not be prepared.") from None
        if len(payload) > MAX_CF_BYTES:
            raise NativeError("io_error", "Device request metadata exceeds the inspection limit.")
        buffer = C.create_string_buffer(payload)
        data = self.data_create(None, buffer, len(payload))
        if not data:
            raise NativeError("io_error", "Device request metadata could not be prepared.")
        ref = None
        try:
            ref = self.plist_create(None, data, 0, None, None)
            if not ref:
                raise NativeError("io_error", "Device request metadata could not be prepared.")
            yield ref
        finally:
            if ref:
                self.release(ref)
            self.release(data)

    def text(self, ref: Any, *, limit: int = 4096) -> str | None:
        if not ref or self.get_type(ref) != self.string_type:
            return None
        length = int(self.string_length(ref))
        if length < 0 or length > limit:
            return None
        maximum = int(self.string_max(length, UTF8))
        if maximum < 0 or maximum > limit * 4:
            return None
        buffer = C.create_string_buffer(maximum + 1)
        if not self.string_get(ref, buffer, len(buffer), UTF8):
            return None
        try:
            return buffer.value.decode("utf-8", errors="strict")
        except UnicodeError:
            return None

    def value(self, ref: Any) -> Any:
        if not ref:
            return None
        data = self.plist_data(None, ref, 100, 0, None)  # XML property list.
        if not data:
            raise NativeError("io_error", "The app returned unsupported metadata.")
        try:
            length = int(self.data_length(data))
            if length < 0 or length > MAX_CF_BYTES:
                raise NativeError("io_error", "App metadata exceeds the inspection limit.")
            pointer = self.data_bytes(data)
            if not pointer and length:
                raise NativeError("io_error", "App metadata could not be read.")
            try:
                return plistlib.loads(C.string_at(pointer, length))
            except (ValueError, OverflowError, plistlib.InvalidFileException):
                raise NativeError("io_error", "App metadata could not be read.") from None
        finally:
            self.release(data)


class _API:
    def __init__(self):
        if sys.platform != "darwin":
            raise NativeError("unsupported", "Direct USB transfer is currently supported on macOS only. Use Apple Devices file sharing on Windows.")
        self.cf = _CoreFoundation()
        self.lib = None
        for path in (
            "/Library/Apple/System/Library/PrivateFrameworks/MobileDevice.framework/MobileDevice",
            "/System/Library/PrivateFrameworks/MobileDevice.framework/MobileDevice",
        ):
            try:
                self.lib = C.CDLL(path)
                break
            except OSError:
                continue
        if self.lib is None:
            raise NativeError("unavailable", "The installed macOS device-support runtime could not be loaded. Use Finder file sharing.")
        # These are intentionally the only device-management functions exposed.
        self.devices = _bind(self.lib, "AMDCreateDeviceList", PTR, [])
        self.interface = _bind(self.lib, "AMDeviceGetInterfaceType", C.c_int, [PTR])
        self.identifier = _bind(self.lib, "AMDeviceCopyDeviceIdentifier", PTR, [PTR])
        self.copy_value = _bind(self.lib, "AMDeviceCopyValue", PTR, [PTR, PTR, PTR])
        self.connect = _bind(self.lib, "AMDeviceConnect", U32, [PTR])
        self.disconnect = _bind(self.lib, "AMDeviceDisconnect", U32, [PTR])
        self.is_paired = _bind(self.lib, "AMDeviceIsPaired", C.c_int, [PTR])
        self.validate = _bind(self.lib, "AMDeviceValidatePairing", U32, [PTR])
        self.start = _bind(self.lib, "AMDeviceStartSession", U32, [PTR])
        self.stop = _bind(self.lib, "AMDeviceStopSession", U32, [PTR])
        self.lookup = _bind(self.lib, "AMDeviceLookupApplications", U32, [PTR, PTR, PPTR])
        self.house_arrest = _bind(self.lib, "AMDeviceCreateHouseArrestService", U32, [PTR, PTR, PTR, PPTR])
        self.afc_close = _bind(self.lib, "AFCConnectionClose", U32, [PTR])
        self.afc_info = _bind(self.lib, "AFCDeviceInfoOpen", U32, [PTR, PPTR])
        self.afc_stat = _bind(self.lib, "AFCFileInfoOpen", U32, [PTR, C.c_char_p, PPTR])
        self.kv_read = _bind(self.lib, "AFCKeyValueRead", U32, [PTR, C.POINTER(C.c_char_p), C.POINTER(C.c_char_p)])
        self.kv_close = _bind(self.lib, "AFCKeyValueClose", U32, [PTR])
        self.dir_open = _bind(self.lib, "AFCDirectoryOpen", U32, [PTR, C.c_char_p, PPTR])
        self.dir_read = _bind(self.lib, "AFCDirectoryRead", U32, [PTR, PTR, C.POINTER(C.c_char_p)])
        self.dir_close = _bind(self.lib, "AFCDirectoryClose", U32, [PTR, PTR])
        self.mkdir = _bind(self.lib, "AFCDirectoryCreate", U32, [PTR, C.c_char_p])
        self.file_open = _bind(self.lib, "AFCFileRefOpen", U32, [PTR, C.c_char_p, U64, C.POINTER(U64)])
        self.file_read = _bind(self.lib, "AFCFileRefRead", U32, [PTR, U64, PTR, C.POINTER(SIZE)])
        self.file_write = _bind(self.lib, "AFCFileRefWrite", U32, [PTR, U64, PTR, SIZE])
        self.file_close = _bind(self.lib, "AFCFileRefClose", U32, [PTR, U64])
        self.file_seek = _bind(self.lib, "AFCFileRefSeek", U32, [PTR, U64, U64, U64])
        self.rename = _bind(self.lib, "AFCRenamePath", U32, [PTR, C.c_char_p, C.c_char_p])
        self.remove = _bind(self.lib, "AFCRemovePath", U32, [PTR, C.c_char_p])
        # AFCConnectionSetIOTimeout's units/ABI are not established here. The
        # isolated worker's hard deadline is mandatory; do not guess a timeout.

    @contextmanager
    def device_list(self):
        array = self.devices()
        if not array:
            yield []
            return
        try:
            if self.cf.get_type(array) != self.cf.array_type:
                raise NativeError("unavailable", "The macOS device-list interface is incompatible.")
            count = int(self.cf.array_count(array))
            if count < 0 or count > MAX_DEVICES:
                raise NativeError("io_error", "Too many device connections were returned.")
            result = []
            for index in range(count):
                device = self.cf.array_value(array, index)
                if device and self.interface(device) == 1:
                    result.append(device)
            # Device pointers are valid only while their owning array remains.
            yield result
        finally:
            self.cf.release(array)

    def opaque_id(self, device) -> str | None:
        ref = self.identifier(device)
        if not ref:
            return None
        try:
            value = self.cf.text(ref, limit=256)
            if not value:
                return None
            return hashlib.sha256(("stillroom-usb:" + value).encode("utf-8")).hexdigest()[:32]
        finally:
            self.cf.release(ref)

    @contextmanager
    def existing_session(self, device):
        connected = started = False
        try:
            _device_check(self.connect(device))
            connected = True
            if self.is_paired(device) != 1:
                raise NativeError("trust_required", "This iPhone needs existing computer trust. No new pairing was attempted.")
            _device_check(self.validate(device), trust=True)
            _device_check(self.start(device))
            started = True
            yield
        finally:
            if started:
                self.stop(device)
            if connected:
                self.disconnect(device)

    def name(self, device) -> str:
        with self.cf.text_ref("DeviceName") as key:
            ref = self.copy_value(device, None, key)
        if not ref:
            return "iPhone"
        try:
            return _safe_text(self.cf.text(ref), "iPhone")
        finally:
            self.cf.release(ref)

    def app_metadata(self, device) -> dict[str, dict]:
        attributes = ["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "CFBundleShortVersionString", "UIFileSharingEnabled"]
        with self.cf.plist_ref({"ReturnAttributes": attributes}) as options:
            result = PTR()
            _device_check(self.lookup(device, options, C.byref(result)))
        if not result.value:
            return {}
        try:
            if self.cf.get_type(result) != self.cf.dict_type:
                raise NativeError("io_error", "Installed-app metadata has an unsupported shape.")
            apps = {}
            # Do not serialize, log or return metadata for unrelated apps.
            for bundle in APP_BUNDLE_IDS:
                with self.cf.text_ref(bundle) as key:
                    app = self.cf.dict_value(result, key)
                if not app:
                    continue
                if self.cf.get_type(app) != self.cf.dict_type:
                    raise NativeError("io_error", "Pocket Stillroom metadata has an unsupported shape.")
                value = self.cf.value(app)
                if isinstance(value, dict):
                    apps[bundle] = value
            return apps
        finally:
            self.cf.release(result)


class NativeBackend:
    """Worker-local backend; no live device action occurs during construction."""

    def __init__(self):
        self._api = _API()
        self._sessions: set[NativeSession] = set()
        self._closed = False

    def _active(self):
        if self._closed:
            raise NativeError("unavailable", "The USB worker has closed.")

    def devices(self) -> list[dict]:
        self._active()
        api = self._api
        rows = []
        seen = set()
        with api.device_list() as devices:
            for device in devices:
                identifier = api.opaque_id(device)
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                row = {"id": identifier, "name": "iPhone", "apps": [], "status": "ready"}
                try:
                    with api.existing_session(device):
                        row["name"] = api.name(device)
                        metadata = api.app_metadata(device)
                        for bundle, app in metadata.items():
                            if app.get("UIFileSharingEnabled") is not True:
                                continue
                            row["apps"].append({
                                "bundleId": bundle,
                                "name": _safe_text(app.get("CFBundleDisplayName") or app.get("CFBundleName"), "Pocket Stillroom"),
                                "version": _safe_text(app.get("CFBundleShortVersionString"), "Unknown", 40),
                            })
                        if not row["apps"]:
                            row["status"] = "app_unavailable"
                            row["message"] = "The approved Pocket Stillroom app with file sharing is not available."
                except NativeError as exc:
                    row["status"] = exc.code if exc.code in {"trust_required", "locked"} else "app_unavailable"
                    row["message"] = exc.message
                rows.append(row)
        return rows

    def open(self, deviceId: str, bundleId: str):
        self._active()
        if bundleId not in APP_BUNDLE_IDS:
            raise NativeError("app_unavailable", "Only the approved Pocket Stillroom app may receive this export.")
        if not isinstance(deviceId, str) or re.fullmatch(r"[0-9a-f]{32}", deviceId) is None:
            raise NativeError("not_found", "Choose an available USB-connected iPhone.")
        api = self._api
        with api.device_list() as devices:
            for device in devices:
                if api.opaque_id(device) != deviceId:
                    continue
                with api.existing_session(device):
                    metadata = api.app_metadata(device).get(bundleId)
                    if metadata is None:
                        raise NativeError("app_unavailable", "The approved Pocket Stillroom app is not installed on this iPhone.")
                    if metadata.get("UIFileSharingEnabled") is not True:
                        raise NativeError("file_sharing_disabled", "The selected app does not expose its Documents through file sharing.")
                    connection = PTR()
                    with api.cf.text_ref(bundleId) as bundle:
                        with api.cf.plist_ref({"Command": "VendDocuments"}) as options:
                            status = api.house_arrest(device, bundle, options, C.byref(connection))
                    if int(status) or not connection.value:
                        if connection.value:
                            api.afc_close(connection)
                        _device_check(status)
                        raise NativeError("app_unavailable", "The selected app Documents could not be opened.")
                    try:
                        session = NativeSession(api, connection.value, self)
                    except BaseException:
                        api.afc_close(connection)
                        raise
                # As in upstream ios-deploy, stop/disconnect the lockdown
                # session after obtaining the independent AFC connection.
                self._sessions.add(session)
                return session
        raise NativeError("not_found", "The selected iPhone is no longer connected by USB.")

    def close(self):
        if self._closed:
            return
        for session in list(self._sessions):
            session.close()
        self._sessions.clear()
        self._closed = True


class NativeSession:
    """One app's verified Documents subtree; mutations are limited to owned files.

    AFC offers no documented exclusive-open/no-replace rename. Fresh unpredictable
    staging/final names, existence checks and a private app subtree reduce the
    race; they do not establish a filesystem-level no-overwrite guarantee.
    """

    def __init__(self, api: _API, connection: int, backend: NativeBackend):
        self._api = api
        self._connection = PTR(connection)
        self._backend = backend
        self._closed = False
        self._owned_dirs: set[str] = set()
        self._owned_files: set[str] = set()
        self._writes: dict[int, str] = {}
        self._reader: tuple[str, int, int] | None = None
        info = self._raw_stat("Documents")
        if info is None or info["type"] != "directory":
            raise NativeError("app_unavailable", "The app Documents boundary could not be verified. No alternate container was requested.")

    def _active(self):
        if self._closed:
            raise NativeError("unavailable", "The app file-sharing connection has closed.")

    def _dictionary(self, pointer) -> dict[str, str]:
        if not pointer:
            raise NativeError("io_error", "The iPhone returned incomplete file metadata.")
        values = {}
        try:
            for _ in range(128):
                key = C.c_char_p()
                value = C.c_char_p()
                _afc_check(self._api.kv_read(pointer, C.byref(key), C.byref(value)))
                if not key.value:
                    return values
                if value.value is None or len(key.value) > 128 or len(value.value) > 4096:
                    raise NativeError("io_error", "The iPhone returned invalid file metadata.")
                try:
                    values[key.value.decode("utf-8")] = value.value.decode("utf-8")
                except UnicodeError:
                    raise NativeError("io_error", "The iPhone returned unsupported file metadata.") from None
            raise NativeError("io_error", "The iPhone returned too much file metadata.")
        finally:
            self._api.kv_close(pointer)

    def _raw_stat(self, path: str) -> dict | None:
        pointer = PTR()
        status = int(self._api.afc_stat(self._connection, path.encode("utf-8"), C.byref(pointer)))
        if status == 8:
            return None
        _afc_check(status)
        values = self._dictionary(pointer)
        kind = {"S_IFREG": "file", "S_IFDIR": "directory", "S_IFLNK": "symlink"}.get(values.get("st_ifmt"))
        if kind is None:
            raise NativeError("invalid_path", "The transfer destination has an unsupported file type.")
        try:
            size = int(values.get("st_size", "0"))
        except (TypeError, ValueError):
            raise NativeError("io_error", "The iPhone returned invalid file-size metadata.") from None
        if size < 0 or size > (1 << 63) - 1:
            raise NativeError("io_error", "The iPhone returned invalid file-size metadata.")
        return {"size": size, "type": kind}

    def _path(self, path: str, *, empty: bool = False) -> str:
        self._active()
        relative = _relative(path, empty=empty)
        parents = ["Documents"]
        parts = relative.split("/") if relative else []
        for index in range(1, len(parts)):
            parents.append("Documents/" + "/".join(parts[:index]))
        for parent in parents:
            info = self._raw_stat(parent)
            if info is None or info["type"] != "directory":
                raise _invalid_path()
        return "Documents" + ("/" + relative if relative else "")

    def free_bytes(self) -> int | None:
        self._path("", empty=True)
        pointer = PTR()
        status = int(self._api.afc_info(self._connection, C.byref(pointer)))
        if status:
            return None
        values = self._dictionary(pointer)
        try:
            free = int(values["FSFreeBytes"])
            return free if 0 <= free <= (1 << 63) - 1 else None
        except (KeyError, TypeError, ValueError):
            return None

    def listdir(self, path: str = "") -> list[str]:
        full = self._path(path, empty=True)
        info = self._raw_stat(full)
        if info is None:
            raise NativeError("not_found", "The transfer directory is no longer available.")
        if info["type"] != "directory":
            raise _invalid_path()
        directory = PTR()
        _afc_check(self._api.dir_open(self._connection, full.encode("utf-8"), C.byref(directory)))
        if not directory.value:
            raise NativeError("io_error", "The transfer directory could not be read.")
        result = []
        try:
            for _ in range(MAX_DIRECTORY_ENTRIES + 2):
                name = C.c_char_p()
                _afc_check(self._api.dir_read(self._connection, directory, C.byref(name)))
                if name.value is None:
                    return result
                if name.value in (b".", b".."):
                    continue
                try:
                    decoded = name.value.decode("utf-8", errors="strict")
                except UnicodeError:
                    raise NativeError("io_error", "The app directory contains an unsupported filename.") from None
                if "/" in decoded:
                    raise _invalid_path()
                _relative(decoded)
                result.append(decoded)
            raise NativeError("io_error", "The app directory exceeds the inspection limit.")
        finally:
            self._api.dir_close(self._connection, directory)

    def stat(self, path: str) -> dict | None:
        return self._raw_stat(self._path(path))

    def mkdir(self, path: str):
        full = self._path(path)
        if re.fullmatch(r"\.stillroom-[0-9a-fA-F-]{16,64}\.partial", path) is None:
            raise _invalid_path()
        if self._raw_stat(full) is not None:
            raise NativeError("exists", "The transfer staging name already exists; it was not reused.")
        _afc_check(self._api.mkdir(self._connection, full.encode("utf-8")))
        info = self._raw_stat(full)
        if info is None or info["type"] != "directory":
            raise NativeError("io_error", "The new transfer directory could not be verified.")
        self._owned_dirs.add(path)

    def open_write(self, path: str) -> int:
        full = self._path(path)
        parts = path.split("/")
        if len(parts) != 2 or parts[0] not in self._owned_dirs:
            raise _invalid_path()
        if self._raw_stat(full) is not None:
            raise NativeError("exists", "The transfer payload already exists; it was not replaced.")
        handle = U64()
        # AFC mode 3 creates/truncates, so it is confined to a fresh UUID-owned
        # directory and a path confirmed absent. It is not an exclusive-open API.
        _afc_check(self._api.file_open(self._connection, full.encode("utf-8"), 3, C.byref(handle)))
        value = int(handle.value)
        if not value or value in self._writes:
            if value:
                self._api.file_close(self._connection, value)
            raise NativeError("io_error", "The new transfer payload could not be opened safely.")
        self._writes[value] = path
        self._owned_files.add(path)
        return value

    def write(self, handle: int, data: bytes):
        self._active()
        if isinstance(handle, bool) or not isinstance(handle, int) or handle not in self._writes:
            raise NativeError("io_error", "The transfer write handle is not owned by this operation.")
        if not isinstance(data, bytes) or len(data) > MAX_CHUNK:
            raise NativeError("io_error", "The transfer chunk exceeds the allowed size.")
        self._path(self._writes[handle])
        if not data:
            return
        buffer = C.create_string_buffer(data)
        _afc_check(self._api.file_write(self._connection, handle, buffer, len(data)))

    def close_file(self, handle: int):
        self._active()
        if isinstance(handle, bool) or not isinstance(handle, int) or handle not in self._writes:
            raise NativeError("io_error", "The transfer write handle is not owned by this operation.")
        del self._writes[handle]
        _afc_check(self._api.file_close(self._connection, handle))

    def _close_reader(self):
        if self._reader is not None:
            _, handle, _ = self._reader
            self._reader = None
            _afc_check(self._api.file_close(self._connection, handle))

    def read(self, path: str, offset: int, size: int) -> bytes:
        full = self._path(path)
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > (1 << 63) - 1
                or isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_CHUNK):
            raise NativeError("io_error", "The transfer read range exceeds the allowed size.")
        info = self._raw_stat(full)
        if info is None or info["type"] != "file":
            raise _invalid_path()
        if not size:
            return b""
        if self._reader is None or self._reader[0] != path:
            self._close_reader()
            handle = U64()
            _afc_check(self._api.file_open(self._connection, full.encode("utf-8"), 1, C.byref(handle)))
            if not handle.value:
                raise NativeError("io_error", "The transfer verification file could not be opened.")
            self._reader = (path, int(handle.value), 0)
        _, handle, current = self._reader
        if current != offset:
            _afc_check(self._api.file_seek(self._connection, handle, offset, 0))
        buffer = C.create_string_buffer(size)
        length = SIZE(size)
        _afc_check(self._api.file_read(self._connection, handle, buffer, C.byref(length)))
        count = int(length.value)
        if count > size:
            raise NativeError("io_error", "The iPhone returned an invalid transfer read length.")
        self._reader = (path, handle, offset + count)
        return buffer.raw[:count]

    def rename(self, old: str, new: str):
        source = self._path(old)
        target = self._path(new)
        if old not in self._owned_files or "/" in new or not new.endswith(".stillroomlibrary"):
            raise _invalid_path()
        if any(path == old for path in self._writes.values()):
            raise NativeError("io_error", "Close the transfer payload before publication.")
        source_info = self._raw_stat(source)
        if source_info is None or source_info["type"] != "file":
            raise _invalid_path()
        if self._raw_stat(target) is not None:
            raise NativeError("exists", "The final transfer name already exists; it was not replaced.")
        self._close_reader()
        _afc_check(self._api.rename(self._connection, source.encode("utf-8"), target.encode("utf-8")))
        self._owned_files.discard(old)
        self._owned_files.add(new)

    def remove(self, path: str):
        full = self._path(path)
        if path not in self._owned_dirs and path not in self._owned_files:
            raise _invalid_path()
        if path in self._writes.values():
            raise NativeError("io_error", "Close the transfer payload before cleanup.")
        info = self._raw_stat(full)
        if info is None:
            self._owned_dirs.discard(path)
            self._owned_files.discard(path)
            return
        if info["type"] == "symlink":
            raise _invalid_path()
        if path in self._owned_dirs and (info["type"] != "directory" or self.listdir(path)):
            raise NativeError("io_error", "The transfer staging directory is not empty; it was retained.")
        if path in self._owned_files and info["type"] != "file":
            raise _invalid_path()
        if self._reader and self._reader[0] == path:
            self._close_reader()
        _afc_check(self._api.remove(self._connection, full.encode("utf-8")))
        self._owned_dirs.discard(path)
        self._owned_files.discard(path)

    def close(self):
        if self._closed:
            return
        # Closing never cleans up or recursively removes phone files. A worker
        # killed by a deadline may leave an incomplete UUID staging directory.
        for handle in list(self._writes):
            self._api.file_close(self._connection, handle)
        self._writes.clear()
        if self._reader is not None:
            self._api.file_close(self._connection, self._reader[1])
            self._reader = None
        self._api.afc_close(self._connection)
        self._closed = True
        self._backend._sessions.discard(self)

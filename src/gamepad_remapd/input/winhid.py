"""Windows: читання HID напряму через WinAPI (ctypes) — БЕЗ сторонніх бібліотек (hidapi збирати не потрібно).

Використовується автоматично на Windows, якщо пакет `hidapi` (модуль `hid`) не встановлено. Дає те саме, що й
hidapi: перелік пристроїв, блокуюче читання звітів, output/feature-звіти. Семантика звіту як у hidraw/hidapi:
Windows завжди додає на початок байт Report ID; якщо пристрій нумерованих звітів не використовує (ID = 0), цей
нульовий байт відкидається — тоді індекси в розкладках збігаються з Linux.

Шар `_Win` — тонкі виклики WinAPI (hid.dll, setupapi.dll, kernel32.dll); шар `WinHidDevice` — логіка поверх нього
(у тестах `_Win` підміняється). УВАГА: `_Win` написано за документацією Microsoft, на Windows ще не запускалось.
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes as wt
from typing import List, Optional, Tuple

from .base import BackendUnavailable, DeviceGone, DeviceInfo, InputDevice

log = logging.getLogger(__name__)

BACKEND = "winhid"
_BT_HID_GUID = "{00001124-0000-1000-8000-00805f9b34fb}"      # сервіс Bluetooth HID у шляху пристрою
_MI = re.compile(r"&mi_([0-9a-f]{2})", re.I)
READ_TIMEOUT_MS = 100


def parse_interface(path: str) -> Optional[int]:
    """`...&mi_02#...` -> 2 (складені USB-пристрої); немає — None."""
    m = _MI.search(path)
    return int(m.group(1), 16) if m else None


def parse_bus(path: str) -> int:
    """Bluetooth HID містить у шляху GUID сервісу 1124; інакше вважаємо USB. (3 = USB, 5 = Bluetooth.)"""
    return 5 if _BT_HID_GUID in path.lower() else 3


class Caps:
    __slots__ = ("usage_page", "usage", "in_len", "out_len", "feat_len")

    def __init__(self, usage_page=0, usage=0, in_len=0, out_len=0, feat_len=0):
        self.usage_page, self.usage = usage_page, usage
        self.in_len, self.out_len, self.feat_len = in_len, out_len, feat_len


# ------------------------------------------------------------------ структури WinAPI
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wt.DWORD),
                ("Reserved", ctypes.c_size_t)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17), ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT), ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT), ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wt.DWORD),
                ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING, ERROR_NO_MORE_ITEMS, ERROR_DEVICE_NOT_CONNECTED = 997, 259, 1167
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
INVALID_HANDLE = ctypes.c_void_p(-1).value


class _Win:
    """Тонкий шар над WinAPI. Лише Windows."""

    def __init__(self) -> None:
        try:
            self.hid = ctypes.WinDLL("hid", use_last_error=True)                 # type: ignore[attr-defined]
            self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)       # type: ignore[attr-defined]
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)            # type: ignore[attr-defined]
        except (AttributeError, OSError) as e:
            raise BackendUnavailable(f"WinAPI HID недоступний на цій ОС: {e}") from e
        V = ctypes.c_void_p
        self.hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
        self.setupapi.SetupDiGetClassDevsW.restype = V
        self.setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, V, wt.DWORD]
        self.setupapi.SetupDiEnumDeviceInterfaces.argtypes = [V, V, ctypes.POINTER(_GUID), wt.DWORD,
                                                              ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [V, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), V,
                                                                   wt.DWORD, ctypes.POINTER(wt.DWORD), V]
        self.setupapi.SetupDiDestroyDeviceInfoList.argtypes = [V]
        self.k32.CreateFileW.restype = V
        self.k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, V, wt.DWORD, wt.DWORD, V]
        self.k32.CloseHandle.argtypes = [V]
        self.k32.CreateEventW.restype = V
        self.k32.CreateEventW.argtypes = [V, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        self.k32.ReadFile.argtypes = [V, V, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(_OVERLAPPED)]
        self.k32.WriteFile.argtypes = [V, V, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(_OVERLAPPED)]
        self.k32.GetOverlappedResult.argtypes = [V, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
        self.k32.WaitForSingleObject.argtypes = [V, wt.DWORD]
        self.k32.CancelIoEx.argtypes = [V, V]
        self.k32.SetEvent.argtypes = [V]
        self.k32.ResetEvent.argtypes = [V]
        for fn in ("HidD_GetAttributes", "HidD_GetProductString", "HidD_GetPreparsedData", "HidD_FreePreparsedData",
                   "HidP_GetCaps", "HidD_SetFeature", "HidD_GetFeature"):
            getattr(self.hid, fn).argtypes = None            # задаємо типи на місці виклику

    # -- перелік
    def paths(self) -> List[str]:
        guid = _GUID()
        self.hid.HidD_GetHidGuid(ctypes.byref(guid))
        info = self.setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if info in (None, INVALID_HANDLE):
            return []
        out: List[str] = []
        try:
            i = 0
            while True:
                data = _SP_DEVICE_INTERFACE_DATA()
                data.cbSize = ctypes.sizeof(data)
                if not self.setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), i, ctypes.byref(data)):
                    break
                need = wt.DWORD(0)
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), None, 0, ctypes.byref(need), None)
                buf = ctypes.create_string_buffer(need.value)
                # SP_DEVICE_INTERFACE_DETAIL_DATA_W: cbSize = 8 у 64-бітному процесі, 6 у 32-бітному
                ctypes.memmove(buf, ctypes.byref(wt.DWORD(8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)), 4)
                if self.setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), buf, need.value, None, None):
                    out.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
                i += 1
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(info)
        return out

    def query(self, path: str) -> Optional[Tuple[int, int, str, Caps]]:
        """vid, pid, назва, caps — без права читання (access = 0), тож працює й для «зайнятих» пристроїв."""
        h = self.k32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID_HANDLE):
            return None
        try:
            at = _HIDD_ATTRIBUTES()
            at.Size = ctypes.sizeof(at)
            if not self.hid.HidD_GetAttributes(ctypes.c_void_p(h), ctypes.byref(at)):
                return None
            name = ctypes.create_unicode_buffer(256)
            self.hid.HidD_GetProductString(ctypes.c_void_p(h), name, ctypes.sizeof(name))
            caps = Caps()
            pp = ctypes.c_void_p()
            if self.hid.HidD_GetPreparsedData(ctypes.c_void_p(h), ctypes.byref(pp)):
                c = _HIDP_CAPS()
                if self.hid.HidP_GetCaps(pp, ctypes.byref(c)) >= 0:
                    caps = Caps(c.UsagePage, c.Usage, c.InputReportByteLength, c.OutputReportByteLength,
                                c.FeatureReportByteLength)
                self.hid.HidD_FreePreparsedData(pp)
            return at.VendorID, at.ProductID, name.value, caps
        finally:
            self.k32.CloseHandle(ctypes.c_void_p(h))

    # -- пристрій
    def open(self, path: str):
        """-> (handle, event) або кидає OSError. Спершу читання+запис, якщо не вийшло — лише читання."""
        for access in (GENERIC_READ | GENERIC_WRITE, GENERIC_READ):
            h = self.k32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                                     FILE_FLAG_OVERLAPPED, None)
            if h not in (None, INVALID_HANDLE):
                ev = self.k32.CreateEventW(None, True, False, None)
                return (h, ev, access & GENERIC_WRITE != 0)
        raise ctypes.WinError(ctypes.get_last_error())           # type: ignore[attr-defined]

    def _cancel_wait(self, h, ov) -> None:
        """Скасувати запит і ДОЧЕКАТИСЬ завершення: поки він «у польоті», ядро пише в наші буфери/OVERLAPPED —
        звільняти їх до цього не можна (інакше segfault)."""
        self.k32.CancelIoEx(ctypes.c_void_p(h), ctypes.byref(ov))
        n = wt.DWORD(0)
        self.k32.GetOverlappedResult(ctypes.c_void_p(h), ctypes.byref(ov), ctypes.byref(n), True)

    def close(self, handle, state=None) -> None:
        h, ev, _ = handle
        if state and state.get("pending"):
            self._cancel_wait(h, state["ov"])
            state["pending"] = False
        self.k32.CloseHandle(ctypes.c_void_p(h))
        self.k32.CloseHandle(ctypes.c_void_p(ev))

    def read(self, state, size: int, timeout_ms: int) -> Optional[bytes]:
        """Одне читання з очікуванням до timeout_ms. None — таймаут/wake (запит лишається «у польоті»)."""
        h, ev, _ = state["handle"]
        if not state.get("pending"):
            state["buf"] = ctypes.create_string_buffer(size)
            state["ov"] = _OVERLAPPED()
            state["ov"].hEvent = ev
            self.k32.ResetEvent(ctypes.c_void_p(ev))
            n = wt.DWORD(0)
            ok = self.k32.ReadFile(ctypes.c_void_p(h), state["buf"], size, ctypes.byref(n), ctypes.byref(state["ov"]))
            if not ok:
                err = ctypes.get_last_error()                    # type: ignore[attr-defined]
                if err != ERROR_IO_PENDING:
                    raise DeviceGone(f"ReadFile: помилка {err}")
            state["pending"] = True
        self.k32.WaitForSingleObject(ctypes.c_void_p(ev), timeout_ms)
        n = wt.DWORD(0)
        # завжди опитуємо стан (wake() теж сигналить подію, а завершення могло прийти між перевірками)
        if not self.k32.GetOverlappedResult(ctypes.c_void_p(h), ctypes.byref(state["ov"]), ctypes.byref(n), False):
            err = ctypes.get_last_error()                        # type: ignore[attr-defined]
            if err == ERROR_IO_INCOMPLETE:
                self.k32.ResetEvent(ctypes.c_void_p(ev))
                return None
            state["pending"] = False
            raise DeviceGone(f"GetOverlappedResult: помилка {err}")
        state["pending"] = False
        return bytes(state["buf"].raw[:n.value])

    def wake(self, state) -> None:
        ev = state["handle"][1]
        self.k32.SetEvent(ctypes.c_void_p(ev))                   # будить WaitForSingleObject

    def write(self, handle, data: bytes) -> None:
        h, ev, can_write = handle
        if not can_write:
            raise PermissionError("пристрій відкрито лише для читання")
        ov = _OVERLAPPED()
        ov.hEvent = ev
        self.k32.ResetEvent(ctypes.c_void_p(ev))
        n = wt.DWORD(0)
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        if not self.k32.WriteFile(ctypes.c_void_p(h), buf, len(data), ctypes.byref(n), ctypes.byref(ov)):
            if ctypes.get_last_error() != ERROR_IO_PENDING:      # type: ignore[attr-defined]
                raise ctypes.WinError(ctypes.get_last_error())   # type: ignore[attr-defined]
            if self.k32.WaitForSingleObject(ctypes.c_void_p(ev), 1000) != WAIT_OBJECT_0:
                self._cancel_wait(h, ov)             # буфери живуть, поки ядро не завершить запит
                raise TimeoutError("WriteFile: таймаут")
            self.k32.GetOverlappedResult(ctypes.c_void_p(h), ctypes.byref(ov), ctypes.byref(n), False)

    def set_feature(self, handle, data: bytes) -> None:
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        if not self.hid.HidD_SetFeature(ctypes.c_void_p(handle[0]), buf, len(data)):
            raise ctypes.WinError(ctypes.get_last_error())       # type: ignore[attr-defined]

    def get_feature(self, handle, report_id: int, length: int) -> bytes:
        buf = ctypes.create_string_buffer(length)
        buf[0] = report_id
        if not self.hid.HidD_GetFeature(ctypes.c_void_p(handle[0]), buf, length):
            raise ctypes.WinError(ctypes.get_last_error())       # type: ignore[attr-defined]
        return bytes(buf.raw)


# ------------------------------------------------------------------ публічний інтерфейс
def enumerate_winhid(win: Optional[_Win] = None) -> List[DeviceInfo]:
    win = win or _Win()
    out: List[DeviceInfo] = []
    for path in win.paths():
        q = win.query(path)
        if q is None:
            continue
        vid, pid, name, caps = q
        out.append(DeviceInfo(path=path, vid=vid, pid=pid, name=name, interface=parse_interface(path),
                              bus=parse_bus(path), backend=BACKEND,
                              extra={"usage_page": caps.usage_page, "usage": caps.usage,
                                     "in_len": caps.in_len, "out_len": caps.out_len, "feat_len": caps.feat_len}))
    return out


class WinHidDevice(InputDevice):
    def __init__(self, info: DeviceInfo, read_size: int = 64, win: Optional[_Win] = None):
        super().__init__(info, read_size)
        self._win = win
        self._st: dict = {}
        self.can_write = True

    def open(self) -> None:
        self._win = self._win or _Win()
        handle = self._win.open(self.info.path)
        self._st = {"handle": handle}
        self.can_write = handle[2]
        # Windows віддає звіт завжди довжиною InputReportByteLength (разом із байтом Report ID)
        n = (self.info.extra or {}).get("in_len") or (self.read_size + 1)
        self._size = max(int(n), 2)

    def close(self) -> None:
        if self._st:
            try:
                self._win.close(self._st["handle"], self._st)
            finally:
                self._st = {}

    def read(self) -> Optional[bytes]:
        if not self._st:
            raise DeviceGone("пристрій закрито")
        data = self._win.read(self._st, self._size, READ_TIMEOUT_MS)
        if not data:
            return None
        if data[0] == 0:                       # нумерованих звітів нема: Windows додав 0x00 попереду — відкидаємо
            data = data[1:]
        return data or None

    def wake(self) -> None:
        if self._st:
            self._win.wake(self._st)

    def send_feature(self, data: bytes) -> None:
        self._win.set_feature(self._st["handle"], data)

    def get_feature(self, report_id: int, length: int) -> bytes:
        return self._win.get_feature(self._st["handle"], report_id, length)

    def write_output(self, data: bytes) -> None:
        out_len = (self.info.extra or {}).get("out_len") or len(data)
        data = bytes(data)
        if len(data) < out_len:                # Windows вимагає звіт повної довжини OutputReportByteLength
            data += bytes(out_len - len(data))
        self._win.write(self._st["handle"], data)

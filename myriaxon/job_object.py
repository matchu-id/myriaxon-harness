"""Windows Job Objects via pure ctypes -- no pywin32, no external install (confirmed live:
CreateJobObjectW/AssignProcessToJobObject/SetInformationJobObject are all directly callable
through ctypes.WinDLL against the stock kernel32.dll).

Gives run_bash two guarantees a real container/VM sandbox would also provide for free:
a hard CPU-time (and optionally memory) cap, and a *guaranteed* whole-process-tree kill.
today's taskkill /F /T is best-effort -- it walks the process tree it can currently see.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is enforced by the kernel itself: every process ever
assigned to the job (including children a member process spawns *after* assignment, since job
membership is inherited unless a child explicitly opts out with CREATE_BREAKAWAY_FROM_JOB) dies
the instant the job handle is closed, with no way to escape or outlive it.
"""

import ctypes
import platform
from ctypes import wintypes

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def job_objects_available() -> bool:
    return platform.system() == "Windows"


class _JobobjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobobjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobobjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ProcessJob:
    """A disposable Job Object meant to hold exactly one run_bash invocation's process tree.
    Raises OSError (via ctypes.WinError) on any Win32 API failure -- callers should treat job
    creation as best-effort and fall back to the existing terminate()/taskkill path if it fails,
    the same never-block-the-user-over-an-extra-safety-layer discipline used elsewhere."""

    def __init__(self, memory_limit_bytes: int | None = None, cpu_time_limit_seconds: float | None = None) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

        info = _JobobjectExtendedLimitInformation()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if cpu_time_limit_seconds is not None:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_TIME
            # PerProcessUserTimeLimit is in 100-nanosecond units.
            info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_time_limit_seconds * 10_000_000)
        if memory_limit_bytes is not None:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = memory_limit_bytes
        info.BasicLimitInformation.LimitFlags = flags

        ok = self._kernel32.SetInformationJobObject(
            self._handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            error = ctypes.WinError(ctypes.get_last_error())
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
            raise error

    def assign(self, pid: int) -> None:
        process_handle = self._kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        """Kills every process still in the job (the whole tree, unconditionally) and releases
        the job handle. Safe to call more than once."""
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "ProcessJob":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

"""Persistent process/shutdown diagnostics for Pokebot3DS-CFW.

The Windows console disappears when RUN_BOT.bat exits, so shutdown-only Python
and Qt errors used to be lost before they could be copied into a support report.
This module tees stderr to a persistent AppData log, installs Python/thread
exception hooks, and captures Qt diagnostic messages.  It is diagnostics-only:
no controller, RAM, or hunt authority depends on it.
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

try:
    from PySide6.QtCore import qInstallMessageHandler
except Exception:  # pragma: no cover - only relevant when Qt import itself fails
    qInstallMessageHandler = None

_LOCK = threading.RLock()
_LOG_FP = None
_ORIGINAL_STDERR = None
_ORIGINAL_SYS_EXCEPTHOOK = None
_ORIGINAL_THREAD_EXCEPTHOOK = None
_PREVIOUS_QT_HANDLER = None
_INSTALLED = False


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_write(text: str) -> None:
    global _LOG_FP
    if _LOG_FP is None:
        return
    try:
        with _LOCK:
            _LOG_FP.write(str(text))
            if not str(text).endswith("\n"):
                _LOG_FP.write("\n")
            _LOG_FP.flush()
    except Exception:
        # Diagnostics must never become an application failure source.
        pass


class _StderrTee:
    def __init__(self, original):
        self._original = original

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")

    def write(self, data):
        text = str(data)
        try:
            if self._original is not None:
                self._original.write(text)
        except Exception:
            pass
        if text:
            try:
                with _LOCK:
                    if _LOG_FP is not None:
                        _LOG_FP.write(text)
                        _LOG_FP.flush()
            except Exception:
                pass
        return len(text)

    def flush(self):
        try:
            if self._original is not None:
                self._original.flush()
        except Exception:
            pass
        try:
            with _LOCK:
                if _LOG_FP is not None:
                    _LOG_FP.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._original and self._original.isatty())
        except Exception:
            return False

    def fileno(self):
        if self._original is None:
            raise OSError("stderr has no file descriptor")
        return self._original.fileno()


def record_event(message: str) -> None:
    _safe_write(f"[{_stamp()}] {message}")


def record_exception(label: str, exc: BaseException) -> None:
    _safe_write(f"[{_stamp()}] {label}: {type(exc).__name__}: {exc}")
    try:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _safe_write(formatted.rstrip())
    except Exception:
        pass


def _sys_excepthook(exc_type, exc_value, exc_tb):
    _safe_write(f"[{_stamp()}] UNHANDLED_MAIN_THREAD_EXCEPTION")
    try:
        _safe_write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)).rstrip())
    except Exception:
        pass
    hook = _ORIGINAL_SYS_EXCEPTHOOK
    if hook is not None:
        try:
            hook(exc_type, exc_value, exc_tb)
        except Exception:
            pass


def _thread_excepthook(args):
    name = getattr(getattr(args, "thread", None), "name", "unknown")
    _safe_write(f"[{_stamp()}] UNHANDLED_BACKGROUND_THREAD_EXCEPTION thread={name}")
    try:
        _safe_write(
            "".join(
                traceback.format_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                )
            ).rstrip()
        )
    except Exception:
        pass
    hook = _ORIGINAL_THREAD_EXCEPTHOOK
    if hook is not None:
        try:
            hook(args)
        except Exception:
            pass


def _qt_message_handler(mode, context, message):
    try:
        mode_name = getattr(mode, "name", str(mode))
    except Exception:
        mode_name = str(mode)
    file_name = getattr(context, "file", None) if context is not None else None
    line = getattr(context, "line", None) if context is not None else None
    function = getattr(context, "function", None) if context is not None else None
    where = ""
    if file_name or line or function:
        where = f" file={file_name!r} line={line!r} function={function!r}"
    _safe_write(f"[{_stamp()}] QT_{mode_name}: {message}{where}")

    # Preserve any handler that was already installed before us.  If Qt was
    # using its default handler, echo to the original console stderr so this
    # diagnostic change does not hide messages from developers/users.
    if _PREVIOUS_QT_HANDLER is not None:
        try:
            _PREVIOUS_QT_HANDLER(mode, context, message)
            return
        except Exception:
            pass
    try:
        if _ORIGINAL_STDERR is not None:
            _ORIGINAL_STDERR.write(f"Qt: {message}\n")
            _ORIGINAL_STDERR.flush()
    except Exception:
        pass


def install(profile_root: Path) -> Path:
    """Install persistent diagnostics once and return the log path."""
    global _INSTALLED, _LOG_FP, _ORIGINAL_STDERR
    global _ORIGINAL_SYS_EXCEPTHOOK, _ORIGINAL_THREAD_EXCEPTHOOK
    global _PREVIOUS_QT_HANDLER

    if _INSTALLED:
        try:
            return Path(_LOG_FP.name)
        except Exception:
            return Path(profile_root) / "logs" / "last_process_console.log"

    log_dir = Path(profile_root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "last_process_console.log"
    # One current-session file is easier for support. Previous diagnostics are
    # already captured in support bundles when needed; do not let this grow forever.
    _LOG_FP = log_path.open("w", encoding="utf-8", buffering=1, errors="replace")

    _ORIGINAL_STDERR = sys.stderr
    sys.stderr = _StderrTee(_ORIGINAL_STDERR)

    _ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        _ORIGINAL_THREAD_EXCEPTHOOK = threading.excepthook
        threading.excepthook = _thread_excepthook

    if qInstallMessageHandler is not None:
        try:
            _PREVIOUS_QT_HANDLER = qInstallMessageHandler(_qt_message_handler)
        except Exception as exc:
            _safe_write(f"[{_stamp()}] QT_MESSAGE_HANDLER_INSTALL_FAILED: {exc}")

    # Also capture fatal interpreter/native faults where Python can provide a
    # traceback. The file stays open for the lifetime of the application.
    try:
        faulthandler.enable(file=_LOG_FP, all_threads=True)
    except Exception as exc:
        _safe_write(f"[{_stamp()}] FAULTHANDLER_INSTALL_FAILED: {exc}")

    _INSTALLED = True
    record_event("PROCESS_DIAGNOSTICS_STARTED")
    return log_path

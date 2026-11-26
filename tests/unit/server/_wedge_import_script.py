"""Subprocess body for ``test_import_wait.py``'s red-green test (FND-5531).

Reproduces, deterministically, how Temporal's deadlock interrupt (TMPRL1101,
delivered with ``PyThreadState_SetAsyncExc``) wedges the import system, then
asks ``WorkerHealthServer.check_live()`` about it. Runs in its own interpreter
because it leaves threads blocked forever.

Usage: ``python _wedge_import_script.py <workdir> interrupt|control``.
Prints one JSON object on stdout.

Sequence:

1. Thread A imports ``m``; m's body blocks on a gate, so A holds m's lock.
2. Thread B imports ``m`` and waits. In ``interrupt`` mode the interrupt is
   queued on B now, as Temporal does once B's activation passes its deadline.
3. A's attempt fails (a cold import that did not finish) and releases m, which
   wakes B. In ``interrupt`` mode B raises right after taking m's ``wakeup``
   lock, so that lock is left held.
4. Thread D retries ``m`` and holds its lock. Thread C imports package ``p``,
   whose ``__init__`` imports ``m``, so C holds p's lock and waits on m.
5. D's attempt succeeds and releases m. In ``interrupt`` mode nothing wakes C:
   it waits forever and keeps p's lock, so ``import p`` hangs everywhere.
"""

import asyncio
import ctypes
import importlib
import importlib._bootstrap as bootstrap
import json
import os
import sys
import threading
import time
import types

from application_sdk.server.health import WorkerHealthServer


class _Interrupt(BaseException):
    """Stands in for Temporal's deadlock-interruption exception."""


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _start_import(name: str) -> threading.Thread:
    def run() -> None:
        try:
            importlib.import_module(name)
        except (_Interrupt, RuntimeError, ModuleNotFoundError):
            pass

    thread = threading.Thread(target=run, name=f"import-{name}", daemon=True)
    thread.start()
    return thread


def _blocked(thread: threading.Thread) -> bool:
    return bool(bootstrap._blocking_on.get(thread.ident))


def _completes(name: str) -> bool:
    thread = _start_import(name)
    thread.join(1.0)
    return not thread.is_alive()


def main() -> None:
    workdir, mode = sys.argv[1], sys.argv[2]
    max_wait = 0.5

    gate = types.ModuleType("_wedge_gate")
    gate.events = []
    gate.fail = True

    def next_event() -> threading.Event:
        event = threading.Event()
        gate.events.append(event)
        return event

    gate.next_event = next_event
    sys.modules["_wedge_gate"] = gate
    with open(os.path.join(workdir, "m.py"), "w") as f:
        f.write(
            "import _wedge_gate\n"
            "_wedge_gate.next_event().wait()\n"
            "if _wedge_gate.fail:\n"
            "    raise RuntimeError('cold import did not finish')\n"
        )
    os.makedirs(os.path.join(workdir, "p"))
    with open(os.path.join(workdir, "p", "__init__.py"), "w") as f:
        f.write("import m\n")
    sys.path.insert(0, workdir)
    importlib.invalidate_caches()

    server = WorkerHealthServer(
        host="127.0.0.1", port=0, max_import_wait_seconds=max_wait
    )

    a = _start_import("m")
    assert _wait_until(lambda: len(gate.events) == 1)
    # In production the contending waiters keep m's lock object alive between
    # attempts; hold it here so the second attempt reuses it.
    m_lock = bootstrap._module_locks["m"]()
    b = _start_import("m")
    assert _wait_until(lambda: _blocked(b))
    if mode == "interrupt":
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(b.ident), ctypes.py_object(_Interrupt)
        )
    gate.events[0].set()
    a.join(5)
    # Without the interrupt, B retries m itself; either way a second attempt
    # is now holding (or about to hold) m.
    if mode == "interrupt":
        b.join(5)
        _start_import("m")
    assert _wait_until(lambda: len(gate.events) == 2)
    c = _start_import("p")
    assert _wait_until(lambda: _blocked(c))
    gate.fail = False
    gate.events[1].set()
    time.sleep(0.3)

    first = asyncio.run(server.check_live())
    time.sleep(max_wait * 2)
    second = asyncio.run(server.check_live())

    result = {
        "wakeup_lock_leaked": m_lock.wakeup.locked(),
        "c_stranded": c.is_alive(),
        "import_p_completes": _completes("p"),
        "fresh_module_import_completes": _completes("_wedge_fresh_probe"),
        "first_live": first.to_dict(),
        "second_live": second.to_dict(),
    }
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    # Stranded threads never exit; skip interpreter shutdown.
    os._exit(0)


if __name__ == "__main__":
    main()

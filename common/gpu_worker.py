"""A model in its own process, which exits when idle so the GPU is fully free again.

Identical in every Luna app that runs a model of its own (Visia's SigLIP 2, Vibia's
CLAP, Lexia's Docling). Freeing a model inside a process leaves its CUDA context, about
300 MB, on the GPU for as long as the process lives; on a 12 GB GPU shared with Luna
Sentia's resident chat model, that is the difference between fitting and not. So the
model lives in a child process, started on the first call and ended after
``idle_seconds`` without one; the app's own process never touches CUDA.

Calls are sent one at a time. An exception raised in the child is raised again in the
app; if the child dies (killed, out of memory), the call fails and the next call
starts a new child.
"""

from __future__ import annotations

import logging
import multiprocessing
import sys
import threading
import time
import types
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)
# How long a call may wait for the child to start and load its model.
START_TIMEOUT_SECONDS = 600.0


class WorkerDiedError(RuntimeError):
    """The model's process ended during a call."""


def _serve(factory: Callable[..., Any], args: tuple, kwargs: dict, connection, idle_seconds: float) -> None:
    """The child: build the model once, answer calls, and exit after ``idle_seconds`` of quiet."""
    model = factory(*args, **kwargs)
    connection.send(("ready", None))
    while connection.poll(idle_seconds):
        try:
            method, call_args, call_kwargs = connection.recv()
        except EOFError:
            return
        try:
            connection.send(("ok", getattr(model, method)(*call_args, **call_kwargs)))
        except BaseException as error:  # noqa: BLE001 - every failure goes back to the app
            try:
                connection.send(("error", error))
            except Exception:  # noqa: BLE001 - the exception itself would not pickle
                connection.send(("error", RuntimeError(f"{type(error).__name__}: {error}")))


class IdleWorker:
    def __init__(self, factory: Callable[..., Any], args: tuple = (), kwargs: dict | None = None,
                 idle_seconds: float = 120.0) -> None:
        self.factory = factory
        self.args = args
        self.kwargs = kwargs or {}
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._process = None
        self._connection = None
        self._last_call = 0.0
        # spawn, not fork: the child must start without the app's threads and state.
        self._context = multiprocessing.get_context("spawn")

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.is_alive()

    def _start(self) -> None:
        parent, child = self._context.Pipe()
        process = self._context.Process(target=_serve, daemon=True, name=f"model-{getattr(self.factory, '__name__', 'worker')}",
                                        args=(self.factory, self.args, self.kwargs, child, self.idle_seconds))
        # A spawned child imports the app's __main__ again, and the apps build themselves
        # (database, routes) when app.main is imported; a blank __main__ keeps the child
        # to the model's own module.
        main = sys.modules["__main__"]
        sys.modules["__main__"] = types.ModuleType("__main__")
        try:
            process.start()
        finally:
            sys.modules["__main__"] = main
        child.close()
        if not parent.poll(START_TIMEOUT_SECONDS):
            process.kill()
            raise WorkerDiedError("The model's process did not start in time.")
        try:
            parent.recv()
        except EOFError as error:
            process.join(5)
            raise WorkerDiedError(f"The model's process could not load its model (exit code {process.exitcode}).") from error
        self._process, self._connection = process, parent
        logger.info("Started %s", process.name)

    def _stop(self) -> None:
        if self._connection is not None:
            self._connection.close()
        if self._process is not None:
            self._process.join(5)
            if self._process.is_alive():
                self._process.kill()
        self._process = self._connection = None

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            # The child times itself out; one about to do so is replaced rather than raced.
            if self.alive and time.monotonic() - self._last_call > self.idle_seconds - 1:
                self._stop()
            if not self.alive:
                self._stop()
                self._start()
            try:
                self._connection.send((method, args, kwargs))
                status, value = self._connection.recv()
            except (EOFError, OSError, BrokenPipeError) as error:
                self._stop()
                raise WorkerDiedError("The model's process ended during the call.") from error
            finally:
                self._last_call = time.monotonic()
            if status == "error":
                raise value
            return value

    def close(self) -> None:
        with self._lock:
            self._stop()

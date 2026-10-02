"""Фоновые задачи: держим ссылки (иначе сборщик мусора может их снять) и пишем ошибки в журнал."""
import asyncio
from typing import Awaitable, Callable

from . import db

_running: set[asyncio.Task] = set()


def spawn(coro: Awaitable, name: str, on_error: Callable[[BaseException], None] | None = None,
          notify: bool = True) -> asyncio.Task:
    """Запустить корутину в фоне. Необработанная ошибка попадает в журнал (и в on_error), а не теряется."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _running.add(task)

    def done(t: asyncio.Task) -> None:
        _running.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        try:
            db.log(f"Фоновая задача «{name}» упала: {type(exc).__name__}: {exc}", "error")
            if on_error:
                on_error(exc)
            if notify:
                from . import notify as notify_mod
                notify_mod.admin(f"❗ Ошибка фоновой задачи «{name}»: {type(exc).__name__}: {exc}"[:500], key=f"task-{name}")
        except Exception:          # журнал недоступен — не роняем цикл
            pass
    task.add_done_callback(done)
    return task


def running() -> int:
    return len(_running)

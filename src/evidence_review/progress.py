"""Progress output while a blocking model request is waiting."""

from contextlib import contextmanager
from threading import Event, Thread
import time


WAIT_INTERVAL = 15


@contextmanager
def model_wait(progress, label):
    if progress is None:
        yield
        return
    stopped = Event()
    started = time.monotonic()

    def notify():
        while not stopped.wait(WAIT_INTERVAL):
            elapsed = int(time.monotonic() - started)
            progress(f"{label}：已等待 {elapsed} 秒（服务尚未返回，可能在加载、排队或生成）")

    thread = Thread(target=notify, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()

"""Background loop: memory sweep, the nightly dream and the opt-in rituals, every few minutes."""

import threading
import time

from app import dream, memory, rituals

TICK_S = 600


def _loop():
    time.sleep(60)  # let the app start
    while True:
        try:
            memory.sweep()
        except Exception as e:
            print(f"  [memory] sweep: {str(e)[:100]}", flush=True)
        try:
            dream.tick()
        except Exception as e:
            print(f"  [dream] {str(e)[:100]}", flush=True)
        try:
            rituals.tick()
        except Exception as e:
            print(f"  [ritual] {str(e)[:100]}", flush=True)
        time.sleep(TICK_S)


def start():
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()

"""Run with python -m scripts.health_worker; restart-safe domain jobs, no LLM or push."""
import argparse
import logging
import signal
import threading
import time

from app.db import SessionLocal, wait_for_db
from app.services.health_companion import maintenance, run_once


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    wait_for_db()
    last_maintenance = 0.0
    while not stop.is_set():
        try:
            worked = run_once(SessionLocal)
            if time.monotonic() - last_maintenance >= 60:
                maintenance(SessionLocal)
                last_maintenance = time.monotonic()
        except Exception:
            logging.error("health_worker_iteration_failed")  # no SQL, payload or secrets
            worked = False
        if args.once:
            break
        if not worked:
            stop.wait(2)


if __name__ == "__main__":
    main()

"""Run the risk monitor standalone (separate from the webhook server).

The webhook server (app.main) already runs the monitor in-process, so you only
need this if you want the risk loop as its OWN supervised process. Both read the
same bot_state.db, so they stay consistent.

    python scripts/run_monitor.py
    python scripts/run_monitor.py --once     # single pass (for testing/cron)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings
from app.orders import OrderEngine
from app.risk import RiskMonitor
from app.state import Store
from app.ts_client import TradeStationClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build() -> RiskMonitor:
    settings = load_settings()
    client = TradeStationClient(settings)
    engine = OrderEngine(settings, client)
    store = Store()
    return RiskMonitor(settings, client, engine, store)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = p.parse_args()

    monitor = build()
    if args.once:
        monitor.tick()
        print("single risk pass complete.")
        return
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()

import os
import json
import asyncio
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict, field

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ----------------------------- Config ------------------------------------

HISTORY_FILE = "static/stock.csv"
TRADE_LOG_FILE = "static/trade_log.txt"

INTERVAL = 1  # interval in seconds, must be a divisor or multiple of 60 (1, 2, 3, 4, 5, 10, 15, 20, 30, 60, 120, ...)
INITIAL_PRICE = 10
MIN_PRICE = 1
MAX_PRICE = 100
BASE_VOLATILITY = 0.03  # intrinsic randomness in the price
EXCHANGE_IMPACT = 0.01  # how much buying/selling affects the price
APPEND_ON_FREEZE = False  # continue the market but at the same price


# ----------------------------- Market Engine ------------------------------

state_lock = threading.RLock()


class MarketEngine:
    def __init__(self):
        self.times = list()
        self.prices = list()
        self.frozen = False

        self.append(time.time(), INITIAL_PRICE)

        if not os.path.exists(TRADE_LOG_FILE):
            open(TRADE_LOG_FILE, "w").close()

    def price(self):
        return self.prices[-1]

    def time(self):
        return self.times[-1]

    def append(self, time, price):
        self.times.append(time)
        self.prices.append(price)

        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time}\t{price}\n")

    def tick(self, now: float):
        with state_lock:
            time = now
            price = self.price()

            if not self.frozen:
                price = self.updatePrice(price)

            if not self.frozen or APPEND_ON_FREEZE:
                self.append(time, price)

    def updatePrice(self, price):
        noise_factor = random.gauss(0, BASE_VOLATILITY)
        price *= (1 + noise_factor)
        price = self.bound_price(price)

        return price

    def buy(self, pdm: float):
        with state_lock:
            price = self.price()
            impact = EXCHANGE_IMPACT * pdm / (1 + price)
            price *= 1 + impact
            price = self.bound_price(price)

            self.prices[-1] = price

    def sell(self, pdm: float):
        with state_lock:
            price = self.price()
            impact = EXCHANGE_IMPACT * pdm / (1 + price)
            price *= 1 - impact
            price = self.bound_price(price)

            self.prices[-1] = price

    def bound_price(self, price):
        return max(MIN_PRICE, min(MAX_PRICE, price))

    def to_dict(self):
        return {
            "times": self.times,
            "prices": self.prices,
            "frozen": self.frozen,
        }


# ----------------------------- WebSocket Manager -------------------------


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)

    async def broadcast(self, message: dict):
        to_remove = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                to_remove.append(ws)
        for ws in to_remove:
            self.active.discard(ws)

# ----------------------------- FastAPI App -------------------------------


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
manager = ConnectionManager()

# ----------------------------- App Routes --------------------------------


@app.get("/")
async def stock_view():
    with open("static/stock.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/operator")
async def operator_view():
    with open("static/operator.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            mtype = msg.get("type")

            if mtype == "freeze":
                engine.frozen = bool(msg.get("freeze", False))
                await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

            if mtype == "buy":
                pdm = float(msg.get("pdm", 0))
                if not engine.frozen:
                    engine.buy(pdm)
                    await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

            if mtype == "sell":
                pdm = float(msg.get("pdm", 0))
                if not engine.frozen:
                    engine.sell(pdm)
                    await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

    except Exception:
        pass
    finally:
        manager.disconnect(websocket)


# ----------------------------- Main --------------------------------------


async def broadcaster_loop():
    if INTERVAL < 60:
        if 60 % INTERVAL != 0:
            raise ValueError("Sub-minute interval must divide 60.")
    else:
        if INTERVAL % 60 != 0:
            raise ValueError("Intervals >= 60 must be multiples of 60 seconds.")

    last_slot = None

    while not server.should_exit:
        now = time.time()
        lt = time.localtime(now)

        if INTERVAL < 60:
            # Sub-minute
            slot = lt.tm_sec // INTERVAL
            key = (lt.tm_hour, lt.tm_min, slot)

        else:
            # Full-minute
            minutes_per_slot = INTERVAL // 60
            slot = lt.tm_min // minutes_per_slot
            key = (lt.tm_hour, slot)

        if key != last_slot:
            last_slot = key
            engine.tick(now)
            await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

        await asyncio.sleep(1.0)

if __name__ == "__main__":
    engine = MarketEngine()

    config = uvicorn.Config(app, host='0.0.0.0', port=8000, log_level='info')
    server = uvicorn.Server(config)
    loop = asyncio.get_event_loop()
    loop.create_task(broadcaster_loop())
    loop.run_until_complete(server.serve())

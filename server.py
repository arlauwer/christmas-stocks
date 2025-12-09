import os
import json
import asyncio
import random
import threading
import time
import numpy as np

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ----------------------------- Config ------------------------------------

HISTORY_FILE = "static/stock.csv"
TRADE_LOG_FILE = "static/trade_log.txt"

INTERVAL = 30  # interval in seconds, must be a divisor or multiple of 60 (1, 2, 3, 4, 5, 10, 15, 20, 30, 60, 120, ...)
BASE_PRICE = 10
MIN_PRICE = 2  # soft limit, will slowly return to INITIAL_PRICE if exceeded
MAX_PRICE = 30  # soft limit, will slowly return to INITIAL_PRICE if exceeded
MU_RETURN = 0.01  # soft return average
BASE_SIGMA = 0.05  # intrinsic randomness in the price
EXCHANGE_IMPACT = 0.005  # how much buying/selling affects the price
APPEND_ON_FREEZE = False  # continue the market but at the same price
LOAD_FROM_FILE = True  # load the history from the csv


# ----------------------------- Market Engine ------------------------------

state_lock = threading.RLock()


class MarketEngine:

    def __init__(self):
        self.times = list()
        self.prices = list()

        self.mu = 0
        self.sigma = BASE_SIGMA

        self.event = Event.make_empty()  # only one event at a time

        self.frozen = False
        self.allow_return = True  # Arnoh
        self.returning = 0  # -1, 0, 1 (down, off, up)

        if LOAD_FROM_FILE:
            self.load_from_file()
        if len(self.times) == 0:
            self.append(time.time(), BASE_PRICE)

        if not os.path.exists(TRADE_LOG_FILE):
            open(TRADE_LOG_FILE, "w").close()

    def load_from_file(self):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f.readlines():
                parts = line.split()
                self.times.append(float(parts[0]))
                self.prices.append(float(parts[1]))

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
                price = self.updatePrice(time, price)

            if not self.frozen or APPEND_ON_FREEZE:
                self.append(time, price)

    def updatePrice(self, time, price):
        mu = self.mu
        if self.allow_return:
            if price > MAX_PRICE:
                self.returning = -1
            if price < MIN_PRICE:
                self.returning = 1
            if self.returning > 0 and price > BASE_PRICE:
                self.returning = 0
            if self.returning < 0 and price < BASE_PRICE:
                self.returning = 0
            mu += self.returning * MU_RETURN
        noise_factor = random.gauss(mu, self.sigma)
        price *= (1 + noise_factor)

        price = self.event.updatePrice(price)

        price = max(min(price, MAX_PRICE*2), MIN_PRICE/2)  # hard limit price

        return price

    def exchange(self, pdm, displayPrice):
        with state_lock:
            price = self.price()
            # impact = pdm * EXCHANGE_IMPACT / (1 + price)
            # price *= 1 + impact

            price *= np.exp2(EXCHANGE_IMPACT * pdm / price)

            self.prices[-1] = price

            with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{self.time()},{pdm},{displayPrice}\n")

    def queue_event(self, event):
        with state_lock:
            if self.event.finished or self.event.priority < event.priority:
                self.event = event
                if event.finished:
                    print("event", event.name, "already finished")
                else:
                    print("event", event.name, "started")
            else:
                print("event", event.name, "ignored")

    def to_dict(self):
        return {
            "times": self.times,
            "prices": self.prices,
            "mu": self.mu,
            "sigma": self.sigma,
            "frozen": self.frozen,
            "allow_return": self.allow_return,
            "returning": self.returning,
            "event": self.event.to_dict(),
        }


# ----------------------------- Events -----------------------------

class Event:
    def __init__(self, name, start_tick, dticks, max_mu, priority=1):
        self.name = name
        self.tick = -start_tick  # tick since start
        self.dticks = dticks  # duration of event in ticks
        self.max_mu = max_mu
        self.priority = priority

        self.finished = False

    def updatePrice(self, price):
        if self.finished:
            return price

        if self.tick >= 0:
            if self.tick >= self.dticks - 1:
                self.finished = True
            mu = self.mu(self.tick / (self.dticks - 1))

            noise_factor = random.gauss(mu, BASE_SIGMA)
            price *= (1 + noise_factor)

        self.tick += 1
        return price

    def mu(self, t):
        # polynomial bump: [0, 1] |-> [0, max_mu]
        x = (t-0.5)**2
        return (1 - x / (0.5**3) + x**2 / 0.5**4) * self.max_mu

    def make_empty():
        event = Event("empty", 0, 0, 0, 0)
        event.started = True
        event.finished = True
        return event

    def to_dict(self):
        return {
            "name": self.name,
            "tick": self.tick,
            "dticks": self.dticks,
            "max_mu": self.max_mu,
            "priority": self.priority,
            "finished": self.finished,
        }


EVENTS = {
    "coffee": Event("coffee", start_tick=3, dticks=10, max_mu=-0.2),
    "grant": Event("grant", start_tick=3, dticks=10, max_mu=0.2),
}

# ----------------------------- WebSocket Manager -------------------------


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)  # Mmmmmmmmmmmmmmmilleke

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

            if mtype == "refresh":
                await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

            if mtype == "exchange":
                pdm = float(msg.get("pdm", 0))
                displayPrice = float(msg.get("displayPrice", 0))
                if not engine.frozen:
                    engine.exchange(pdm, displayPrice)
                    await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

            if mtype == "freeze":
                engine.frozen = not engine.frozen
                await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

            if mtype == "allow-return":
                engine.allow_return = not engine.allow_return
                await manager.broadcast({"type": "engine", "engine": engine.to_dict()})

            if mtype == "event":
                event = EVENTS[msg.get("event")]
                engine.queue_event(event)

            if mtype == "mu":
                engine.mu = msg.get("mu")

            if mtype == "sigma":
                sigma = msg.get("sigma")
                if sigma <= 0:
                    sigma = BASE_SIGMA
                engine.sigma = sigma

    except Exception:
        pass
    finally:
        manager.disconnect(websocket)


# ----------------------------- Main --------------------------------------


async def broadcaster_loop():
    if INTERVAL < 60:
        if 60 % INTERVAL != 0:  # Boh
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

        if key != last_slot:  # Leneh
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


"""
- add low/high ints for pdm selling
- add up/down button for manual control
- set price to global stock of silver/pdm -> problem buy/sell is nonlinear?
- operator/ add max/min exchange to fail the market, for me to see and not break the market
- make sure net price change = 0 for many exchanges!!!!!
"""

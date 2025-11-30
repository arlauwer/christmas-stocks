import json
import asyncio
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ----------------------------- Config ------------------------------------

HISTORY_FILE = "static/stock.csv"
MAX_HISTORY_POINTS = 1000

INITIAL_PRICE = 10.0
MIN_PRICE = 1.0
MAX_PRICE = 10000.0
BASE_VOLATILITY = 0.03
PRICE_IMPACT_COEFF = 0.01

# ----------------------------- State Classes -----------------------------


@dataclass
class MarketState:
    price: float = INITIAL_PRICE
    time: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.price = d.get("price", INITIAL_PRICE)
        s.time = d.get("time", time.time())
        return s


# ----------------------------- Market Engine ------------------------------

state_lock = threading.RLock()


class MarketEngine:
    def __init__(self, state: MarketState):
        self.state = state
        self.last_tick = time.time()
        self.history = deque(maxlen=MAX_HISTORY_POINTS)
        self.frozen = False

    def bound_price(self):
        self.state.price = max(MIN_PRICE, min(MAX_PRICE, self.state.price))

    def buy(self, pdm: float):
        with state_lock:
            impact = PRICE_IMPACT_COEFF * pdm / (1 + self.state.price)
            self.state.price *= (1 + impact)
            self.bound_price()

    def sell(self, pdm: float):
        with state_lock:
            impact = PRICE_IMPACT_COEFF * pdm / (1 + self.state.price)
            self.state.price *= max(0.01, 1 - impact)
            self.bound_price()

    def _tick(self, now: float):
        if self.frozen:
            return
        with state_lock:
            self.state.time = now

            noise_factor = random.gauss(0, BASE_VOLATILITY)
            self.state.price *= (1 + noise_factor)
            self.bound_price()

            self.history.append((self.state.time, self.state.price))

            self._append_to_csv()

    def _append_to_csv(self):
        # Format time as HH:MM (24-hour) based on local time
        timestamp = datetime.fromtimestamp(self.state.time).strftime("%H:%M")
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{timestamp}\t{self.state.price}\n")


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

            if mtype == "buy":
                pdm = float(msg.get("pdm", 0))
                if not engine.frozen:
                    engine.buy(pdm)
                await manager.broadcast({"type": "buy", "pdm": pdm, "price": engine.state.price})
                await manager.broadcast({"type": "state", "state": engine.state.to_dict()})

            elif mtype == "sell":
                pdm = float(msg.get("pdm", 0))
                if not engine.frozen:
                    engine.sell(pdm)
                await manager.broadcast({"type": "sell", "pdm": pdm, "price": engine.state.price})
                await manager.broadcast({"type": "state", "state": engine.state.to_dict()})

            elif mtype == "freeze":
                engine.frozen = bool(msg.get("freeze", False))
                await manager.broadcast({"type": "frozen", "frozen": engine.frozen})

            else:
                pass

    except Exception:
        pass
    finally:
        manager.disconnect(websocket)


# ----------------------------- Main --------------------------------------

state = MarketState()
engine = MarketEngine(state)

config = uvicorn.Config(app, host='0.0.0.0', port=8000, log_level='info')
server = uvicorn.Server(config)
loop = asyncio.get_event_loop()


async def broadcaster_loop():

    last_minute = None

    while not server.should_exit:
        now = time.localtime()
        current_minute = (now.tm_hour, now.tm_min)

        # if last_minute != current_minute:
        if True:
            last_minute = current_minute

            now = time.time()
            engine._tick(now)
            engine.last_tick = now
            await manager.broadcast({"type": "state", "state": engine.state.to_dict()})

        await asyncio.sleep(1.0)

loop.create_task(broadcaster_loop())
loop.run_until_complete(server.serve())

# rsi_long_bot.py
import time
import hmac
import hashlib
import requests
import json
from flask import Flask, request, jsonify
import os

API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY", "YOUR_BINANCE_SECRET_KEY")
BASE_URL = "https://testnet.binancefuture.com"

app = Flask(__name__)

recent_signals = {}

def has_open_position(symbol):
    r = requests.get(
        BASE_URL + "/fapi/v2/positionRisk",
        headers={"X-MBX-APIKEY": API_KEY}
    ).json()
    pos = next((p for p in r if p["symbol"] == symbol), None)
    return abs(float(pos["positionAmt"])) > 0 if pos else False

def get_total_open_position_value():
    r = requests.get(
        BASE_URL + "/fapi/v2/positionRisk",
        headers={"X-MBX-APIKEY": API_KEY}
    ).json()
    return sum([
        abs(float(p["positionAmt"])) * float(p["markPrice"])
        for p in r if abs(float(p["positionAmt"])) > 0
    ])

def get_total_balance():
    r = requests.get(
        BASE_URL + "/fapi/v2/balance",
        headers={"X-MBX-APIKEY": API_KEY}
    ).json()
    usdt = next((b for b in r if b["asset"] == "USDT"), {"balance": 0})
    return float(usdt["balance"])

def get_order_quantity(symbol, entry_price, leverage, position_pct):
    total_balance = get_total_balance()
    qty = (total_balance * position_pct * leverage) / entry_price
    return round(qty, 2)

def send_order(symbol, side, entry_price, tp_pct, sl_pct, position_pct, leverage):
    quantity = get_order_quantity(symbol, entry_price, leverage, position_pct)

    order_params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": quantity,
        "price": round(entry_price, 4),
        "recvWindow": 5000,
        "timestamp": int(time.time() * 1000)
    }
    query_string = "&".join([f"{k}={v}" for k, v in order_params.items()])
    signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    order_params["signature"] = signature

    response = requests.post(
        BASE_URL + "/fapi/v1/order",
        headers={"X-MBX-APIKEY": API_KEY},
        params=order_params
    )
    print("[ORDER RESPONSE]", response.json())

    tp_price = round(entry_price * (1 + tp_pct / 100), 4)
    sl_price = round(entry_price * (1 - sl_pct / 100), 4)

    for order_type, target_price in [("TP", tp_price), ("SL", sl_price)]:
        cond_order = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_MARKET" if order_type == "SL" else "TAKE_PROFIT_MARKET",
            "stopPrice": target_price,
            "closePosition": True,
            "timeInForce": "GTC",
            "timestamp": int(time.time() * 1000)
        }
        q_string = "&".join([f"{k}={v}" for k, v in cond_order.items()])
        sign = hmac.new(API_SECRET.encode(), q_string.encode(), hashlib.sha256).hexdigest()
        cond_order["signature"] = sign

        r = requests.post(
            BASE_URL + "/fapi/v1/order",
            headers={"X-MBX-APIKEY": API_KEY},
            params=cond_order
        )
        print(f"[{order_type} RESPONSE]", r.json())

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("[WEBHOOK RECEIVED]", data)

    if data.get("strategy") == "rsi_divergence" and data.get("signal") == "bull":
        symbol = data.get("ticker", "ADAUSDT")
        price = float(data.get("price"))
        now = int(time.time() * 1000)

        if has_open_position(symbol):
            print(f"[SKIPPED] Position exists for {symbol}.")
            return jsonify({"status": "skipped (open position)"}), 200

        total_balance = get_total_balance()
        open_value = get_total_open_position_value()
        if open_value / total_balance > 0.5:
            print("[SKIPPED] Risk limit exceeded (50%).")
            return jsonify({"status": "skipped (risk limit)"}), 200

        prev_time = recent_signals.get(symbol)
        recent_signals[symbol] = now
        position_pct = 0.10

        if prev_time and (now - prev_time) < 90 * 60 * 1000:
            print(f"[STRONG SIGNAL] Double divergence for {symbol}. 20% entry.")
            position_pct = 0.20

        send_order(
            symbol=symbol,
            side="BUY",
            entry_price=price,
            tp_pct=2.5,
            sl_pct=1.0,
            position_pct=position_pct,
            leverage=2
        )

    return jsonify({"status": "received"}), 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

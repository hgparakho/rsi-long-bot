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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = Flask(__name__)

recent_signals = {}

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text}
        )
    except Exception as e:
        print("[ERROR] Telegram failed:", str(e))

def has_open_position(symbol):
    try:
        r = requests.get(
            BASE_URL + "/fapi/v2/positionRisk",
            headers={"X-MBX-APIKEY": API_KEY}
        ).json()
        pos = next((p for p in r if p["symbol"] == symbol), None)
        return abs(float(pos["positionAmt"])) > 0 if pos else False
    except Exception as e:
        print("[ERROR] Failed to check position:", str(e))
        return False

def get_total_open_position_value():
    try:
        r = requests.get(
            BASE_URL + "/fapi/v2/positionRisk",
            headers={"X-MBX-APIKEY": API_KEY}
        ).json()
        return sum([
            abs(float(p["positionAmt"])) * float(p["markPrice"])
            for p in r if abs(float(p["positionAmt"])) > 0
        ])
    except Exception as e:
        print("[ERROR] Failed to get position value:", str(e))
        return 0

def get_total_balance():
    try:
        r = requests.get(
            BASE_URL + "/fapi/v2/balance",
            headers={"X-MBX-APIKEY": API_KEY}
        ).json()
        usdt = next((b for b in r if b["asset"] == "USDT"), {"balance": 0})
        return float(usdt["balance"])
    except Exception as e:
        print("[ERROR] Failed to get balance:", str(e))
        return 0

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

    try:
        response = requests.post(
            BASE_URL + "/fapi/v1/order",
            headers={"X-MBX-APIKEY": API_KEY},
            params=order_params
        )
        result = response.json()
        print("[ORDER RESPONSE]", result)
        if "orderId" not in result:
            print("[ERROR] Order failed to place")
            send_telegram_message(f"❌ 주문 실패: {symbol} - {result}")
            return False
    except Exception as e:
        print("[EXCEPTION] Order placement failed:", str(e))
        send_telegram_message(f"⚠️ 주문 오류: {symbol} - {str(e)}")
        return False

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

        try:
            r = requests.post(
                BASE_URL + "/fapi/v1/order",
                headers={"X-MBX-APIKEY": API_KEY},
                params=cond_order
            )
            print(f"[{order_type} RESPONSE]", r.json())
        except Exception as e:
            print(f"[EXCEPTION] {order_type} order failed:", str(e))
            send_telegram_message(f"⚠️ {order_type} 설정 실패: {symbol} - {str(e)}")

    send_telegram_message(f"✅ 진입 완료: {symbol} - {side} / 진입가: {entry_price} / 수량: {quantity}")
    return True

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
        if open_value / total_balance > 1.0:
            print("[SKIPPED] Risk limit exceeded (100%).")
            return jsonify({"status": "skipped (risk limit)"}), 200

        prev_time = recent_signals.get(symbol)
        recent_signals[symbol] = now
        position_pct = 0.10

        if prev_time and (now - prev_time) < 90 * 60 * 1000:
            print(f"[STRONG SIGNAL] Double divergence for {symbol}. 20% entry.")
            position_pct = 0.20

        success = send_order(
            symbol=symbol,
            side="BUY",
            entry_price=price,
            tp_pct=3.5,
            sl_pct=1.0,
            position_pct=position_pct,
            leverage=2
        )

        if not success:
            print("[FAILED] Order placement failed.")
            return jsonify({"status": "error (order failed)"}), 500

    return jsonify({"status": "received"}), 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

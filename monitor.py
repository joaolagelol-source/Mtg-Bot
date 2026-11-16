#!/usr/bin/env python3
"""Monitor de promoções de Magic: The Gathering na Amazon Brasil -> Telegram."""
import json
import os
import random
import re
import statistics
import sys
import time
from html import escape

import requests
from bs4 import BeautifulSoup

BASE = "https://www.amazon.com.br"
QUERIES = [
    "magic the gathering booster",
    "magic the gathering commander deck",
    "magic the gathering bundle",
    "magic the gathering box",
    "magic the gathering sleeves",
    "magic the gathering playmat",
    "mtg",
]
MIN_DISCOUNT = float(os.getenv("MIN_DISCOUNT", "25"))  # desconto mínimo (%)
MAX_ALERTS_PER_RUN = 5
MIN_SAMPLES = 5            # amostras de histórico antes de confiar na mediana
SAMPLE_EVERY = 6 * 3600    # grava um preço a cada 6h
MAX_SAMPLES = 80
REALERT_AFTER = 48 * 3600  # não repete o mesmo alerta por 48h
STATE_FILE = "state.json"
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
MTG = re.compile(r"magic.{0,12}gathering|\bmtg\b", re.I)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("prices", {})
    return s


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, sort_keys=True)


def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30
    )
    if not r.ok:
        print("Erro Telegram:", r.status_code, r.text[:200])
        return {}
    return r.json()


def find_chat(state):
    if state.get("chat_id"):
        return state["chat_id"]
    for u in reversed(tg("getUpdates").get("result", [])):
        msg = u.get("message")
        if msg:
            state["chat_id"] = msg["chat"]["id"]
            tg(
                "sendMessage",
                chat_id=state["chat_id"],
                text="✅ Bot ativo! Vou avisar aqui quando achar promoções de "
                "Magic: The Gathering na Amazon.",
            )
            return state["chat_id"]
    print("Mande /start para o seu bot no Telegram e rode de novo.")
    return None


def parse_price(text):
    m = re.search(r"(\d[\d.]*,\d{2})", text or "")
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def brl(x):
    return "R$ " + f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def is_blocked(resp):
    t = resp.text.lower()
    return resp.status_code != 200 or "captcha" in t or "api-services-support" in t


def parse(page):
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for div in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = div.get("data-asin")
        h2 = div.select_one("h2")
        title = h2.get_text(" ", strip=True) if h2 else ""
        if not asin or not MTG.search(title):
            continue
        p = div.select_one("span.a-price:not(.a-text-price) span.a-offscreen")
        price = parse_price(p.get_text()) if p else None
        if not price:
            continue
        lp = div.select_one("span.a-price.a-text-price span.a-offscreen")
        out.append(
            {
                "asin": asin,
                "title": title,
                "price": price,
                "list_price": parse_price(lp.get_text()) if lp else None,
            }
        )
    return out


def main():
    if not TOKEN:
        sys.exit("Faltou o secret TELEGRAM_TOKEN (veja o README).")
    state = load_state()
    chat = find_chat(state)
    if not chat:
        save_state(state)
        return

    items, blocked = {}, 0
    for q in QUERIES:
        try:
            r = requests.get(f"{BASE}/s", params={"k": q}, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print("Falha de rede:", e)
            blocked += 1
            continue
        if is_blocked(r):
            print(f"Bloqueio/erro em '{q}' (HTTP {r.status_code})")
            blocked += 1
        else:
            for it in parse(r.text):
                items[it["asin"]] = it
        time.sleep(random.uniform(2, 5))

    now = time.time()
    if blocked == len(QUERIES):
        if now - state.get("blocked_alert", 0) > 6 * 3600:
            tg(
                "sendMessage",
                chat_id=chat,
                text="⚠️ A Amazon bloqueou as consultas. Vou continuar tentando; "
                "se persistir, veja a seção 'Se a Amazon bloquear' no README.",
            )
            state["blocked_alert"] = now
        save_state(state)
        return

    deals = []
    for asin, it in items.items():
        h = state["prices"].setdefault(asin, {"title": it["title"], "hist": [], "alert": None})
        hist = h["hist"]
        if len(hist) >= MIN_SAMPLES:
            ref = statistics.median(p for _, p in hist)
        else:
            ref = it["list_price"]
        if ref and ref > it["price"]:
            disc = (1 - it["price"] / ref) * 100
            last = h["alert"]
            repeated = last and it["price"] >= last["price"] * 0.99 and now - last["ts"] < REALERT_AFTER
            if disc >= MIN_DISCOUNT and not repeated:
                deals.append((disc, ref, it))
        if not hist or now - hist[-1][0] >= SAMPLE_EVERY:
            hist.append([int(now), it["price"]])
            del hist[:-MAX_SAMPLES]

    deals.sort(key=lambda d: d[0], reverse=True)
    for disc, ref, it in deals[:MAX_ALERTS_PER_RUN]:
        msg = (
            f"🔥 <b>-{disc:.0f}%</b> — {escape(it['title'][:200])}\n"
            f"💰 {brl(it['price'])} (referência: {brl(ref)})\n"
            f"<a href=\"{BASE}/dp/{it['asin']}\">Abrir na Amazon</a>"
        )
        tg("sendMessage", chat_id=chat, text=msg, parse_mode="HTML")
        state["prices"][it["asin"]]["alert"] = {"price": it["price"], "ts": now}

    print(f"{len(items)} produtos vistos, {len(deals)} promoções, {blocked} consultas bloqueadas")
    save_state(state)


if __name__ == "__main__":
    main()

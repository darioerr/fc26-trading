from flask import Flask, render_template, jsonify, request
import requests
import threading
import time
import random
import os
import re
import sqlite3
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
BUDGET_MIN = 200000
BUDGET_MAX = 500000

# ─────────────────────────────────────────────
# MOCK DATA — caricato subito
# ─────────────────────────────────────────────
MOCK_PLAYERS = [
    {"name":"Vinícius Jr.", "price":147000,"rating":"91","version":"TOTW",    "position":"LW", "change":"+9%", "change_pct":9.0, "signal":"🔥 SPIKE",      "source":"mock"},
    {"name":"Pedri",        "price": 88000,"rating":"88","version":"Oro",     "position":"CM", "change":"+5%", "change_pct":5.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"Bellingham",   "price":325000,"rating":"93","version":"Fanta FC","position":"CAM","change":"+13%","change_pct":13.0,"signal":"🔥 SPIKE",      "source":"mock"},
    {"name":"Militão",      "price": 72000,"rating":"87","version":"Oro",     "position":"CB", "change":"+8%", "change_pct":8.0, "signal":"🔥 SPIKE",      "source":"mock"},
    {"name":"Rüdiger",      "price": 58000,"rating":"86","version":"Oro",     "position":"CB", "change":"+6%", "change_pct":6.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"Musiala",      "price": 93000,"rating":"88","version":"TOTW",    "position":"CAM","change":"+7%", "change_pct":7.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"Saka",         "price": 79000,"rating":"87","version":"Oro",     "position":"RW", "change":"+4%", "change_pct":4.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"Yamal",        "price":215000,"rating":"91","version":"Fanta FC","position":"RW", "change":"+16%","change_pct":16.0,"signal":"🔥 SPIKE",      "source":"mock"},
    {"name":"Wirtz",        "price":112000,"rating":"89","version":"TOTW",    "position":"CAM","change":"+10%","change_pct":10.0,"signal":"🔥 SPIKE",      "source":"mock"},
    {"name":"Salah",        "price":145000,"rating":"90","version":"TOTW",    "position":"RW", "change":"+11%","change_pct":11.0,"signal":"🔥 SPIKE",      "source":"mock"},
    {"name":"Mbappé",       "price":480000,"rating":"94","version":"Fanta FC","position":"ST", "change":"+5%", "change_pct":5.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"Haaland",      "price":310000,"rating":"93","version":"TOTW",    "position":"ST", "change":"+7%", "change_pct":7.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"Kimmich",      "price": 48000,"rating":"86","version":"Oro",     "position":"CDM","change":"+4%", "change_pct":4.0, "signal":"🟢 IN SALITA", "source":"mock"},
    {"name":"De Bruyne",    "price":195000,"rating":"91","version":"Oro",     "position":"CAM","change":"+3%", "change_pct":3.0, "signal":"🟡 ATTENZIONE","source":"mock"},
    {"name":"Dembélé",      "price": 55000,"rating":"85","version":"Oro",     "position":"RW", "change":"+6%", "change_pct":6.0, "signal":"🟢 IN SALITA", "source":"mock"},
]

MOCK_SBCS = [
    {"name":"POTM Bundesliga",        "reward":"Rare Mega Pack",          "expiry":"5 giorni",  "cost_estimate":"85K"},
    {"name":"Fondamenta Liga",         "reward":"Prime Gold Players Pack", "expiry":"2 giorni",  "cost_estimate":"45K"},
    {"name":"Icona Base",              "reward":"Icona Base Pick",         "expiry":"Permanente","cost_estimate":"350K"},
    {"name":"Upgrade Rari",            "reward":"Jumbo Premium Gold",      "expiry":"3 giorni",  "cost_estimate":"25K"},
    {"name":"Squadra della Settimana", "reward":"Mega Pack",               "expiry":"6 giorni",  "cost_estimate":"120K"},
]

def build_signals(cards):
    signals = []
    for card in cards:
        price = card.get("price", 0)
        if BUDGET_MIN // 15 <= price <= int(BUDGET_MAX * 0.85):
            pct = card.get("change_pct", 5.0)
            signals.append({
                "type": "BUY",
                "card": card["name"],
                "version": card.get("version", "Oro"),
                "position": card.get("position", "?"),
                "rating": card.get("rating", "?"),
                "price": price,
                "reason": f"Trend +{pct}% nelle ultime 3h",
                "confidence": "Alta" if pct >= 8 else "Media" if pct >= 4 else "Bassa",
                "target_sell": int(price * random.uniform(1.08, 1.20)),
                "source": card.get("source", "?"),
            })
    return sorted(signals, key=lambda x: x["confidence"] == "Alta", reverse=True)[:10]

def build_role_trends(cards):
    roles = {}
    for c in cards:
        pos = c.get("position", "?")
        pct = c.get("change_pct", 0)
        if pos not in roles:
            roles[pos] = {"pcts": [], "count": 0, "prices": []}
        roles[pos]["pcts"].append(pct)
        roles[pos]["count"] += 1
        roles[pos]["prices"].append(c.get("price", 0))
    result = []
    for pos, data in roles.items():
        avg_pct = round(sum(data["pcts"]) / len(data["pcts"]), 1)
        avg_price = int(sum(data["prices"]) / len(data["prices"]))
        result.append({
            "position": pos,
            "change_3h": avg_pct,
            "change_24h": round(avg_pct * 0.7, 1),
            "avg_price": avg_price,
            "card_count": data["count"],
            "signal": "🔥 SPIKE" if avg_pct > 8 else "🟢 IN SALITA" if avg_pct > 2 else "⚪ STABILE"
        })
    return sorted(result, key=lambda x: x["change_3h"], reverse=True)

# ─────────────────────────────────────────────
# CACHE — parte subito con mock
# ─────────────────────────────────────────────
cache = {
    "trending": MOCK_PLAYERS,
    "top_movers": sorted(MOCK_PLAYERS, key=lambda x: x["change_pct"], reverse=True)[:10],
    "role_trends": build_role_trends(MOCK_PLAYERS),
    "sbc_picks": MOCK_SBCS,
    "signals": build_signals(MOCK_PLAYERS),
    "ai_analysis": "⏳ Analisi AI in caricamento... (richiede ~60 secondi al primo avvio)",
    "leak_analysis": "⏳ Previsioni in caricamento...",
    "category_analysis": {
        "categories": [],
        "top_tip": "⏳ Caricamento...",
        "da_evitare": "⏳ Caricamento...",
        "alert_spike": [],
        "sbc_analysis": [],
        "market_summary": ""
    },
    "leaks": [],
    "data_source": "mock",
    "last_update": datetime.now().strftime("%H:%M:%S"),
    "total_cards": len(MOCK_PLAYERS),
    "db_records": 0,
    "loading": False,
}

# ─────────────────────────────────────────────
# GROQ AI
# ─────────────────────────────────────────────
def ask_groq(prompt, max_tokens=2000):
    if not GROQ_API_KEY:
        return None, "⚠️ GROQ_API_KEY non configurata."
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": 0.6},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"], None
    except Exception as e:
        return None, f"⚠️ Groq: {str(e)}"

def run_ai_analysis():
    cards = cache["trending"]
    sbcs = cache["sbc_picks"]

    cards_text = "\n".join([
        f"- {c['name']} ({c.get('version','Oro')} {c.get('rating','?')} {c.get('position','?')}): {c['price']:,}cr, {c.get('change','')}%"
        for c in cards[:12]
    ])
    sbc_text = "\n".join([f"- {s['name']}: ~{s['cost_estimate']}, scade {s['expiry']}" for s in sbcs[:5]])

    prompt = f"""Sei un esperto trader FC26 Ultimate Team. Budget utente: 200K-500K crediti PS.

CARTE IN TREND:
{cards_text}

SBC ATTIVE:
{sbc_text}

Rispondi in italiano con:
1. TOP 3 ACQUISTI CONSIGLIATI con prezzo entry e target di vendita
2. CARTE DA EVITARE
3. STRATEGIA SBC più conveniente
4. CONSIGLIO DEL GIORNO

Sii diretto e usa emoji."""

    result, err = ask_groq(prompt, 1500)
    if result:
        cache["ai_analysis"] = result
    else:
        cache["ai_analysis"] = err or "⚠️ Analisi non disponibile"

    prompt2 = """Sei un esperto di FC26 Ultimate Team.
Quali carte tendono a salire PRIMA degli annunci ufficiali di promozioni, TOTW, SBC ed Evoluzioni?
Dai 5 consigli pratici su cosa comprare in anticipo. Rispondi in italiano con emoji."""

    result2, _ = ask_groq(prompt2, 800)
    if result2:
        cache["leak_analysis"] = result2

    cache["last_update"] = datetime.now().strftime("%H:%M:%S")
    print(f"✅ AI analysis completata alle {cache['last_update']}")

# ─────────────────────────────────────────────
# SCRAPING (opzionale, in background)
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}

def try_scrape_futgg():
    try:
        url = "https://www.fut.gg/players/?order_by=-price_change_percentage_24h&platform=ps"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        cards = []
        for card in soup.select("[class*='player']")[:20]:
            try:
                name = card.select_one("[class*='name'], h3, h4")
                price = card.select_one("[class*='price']")
                rating = card.select_one("[class*='rating'], [class*='ovr']")
                if name and price:
                    price_val = int(''.join(filter(str.isdigit, price.get_text(strip=True))))
                    if price_val > 1000:
                        cards.append({
                            "name": name.get_text(strip=True),
                            "price": price_val,
                            "rating": rating.get_text(strip=True) if rating else "?",
                            "version": "Oro",
                            "position": "?",
                            "change": f"+{random.randint(2,12)}%",
                            "change_pct": random.uniform(2, 12),
                            "signal": "🟢 IN SALITA",
                            "source": "fut.gg"
                        })
            except:
                continue
        return cards
    except:
        return []

def background_loop():
    # Prima cosa: analisi AI con dati mock
    time.sleep(5)
    print("🤖 Avvio analisi AI...")
    try:
        run_ai_analysis()
    except Exception as e:
        print(f"⚠️ AI error: {e}")

    # Poi prova scraping reale ogni 15 minuti
    while True:
        time.sleep(900)
        print("🔄 Tentativo scraping reale...")
        try:
            real_cards = try_scrape_futgg()
            if len(real_cards) >= 5:
                cache["trending"] = real_cards
                cache["top_movers"] = sorted(real_cards, key=lambda x: x["change_pct"], reverse=True)[:10]
                cache["role_trends"] = build_role_trends(real_cards)
                cache["signals"] = build_signals(real_cards)
                cache["data_source"] = "fut.gg"
                cache["total_cards"] = len(real_cards)
                print(f"✅ Dati reali: {len(real_cards)} carte")
            else:
                print("⚠️ Scraping insufficiente, mantengo mock")
        except Exception as e:
            print(f"⚠️ Scraping error: {e}")

        try:
            run_ai_analysis()
        except Exception as e:
            print(f"⚠️ AI refresh error: {e}")

        cache["last_update"] = datetime.now().strftime("%H:%M:%S")

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def get_data():
    return jsonify(cache)

@app.route("/api/refresh")
def refresh():
    threading.Thread(target=run_ai_analysis, daemon=True).start()
    return jsonify({"status": "refreshing"})

@app.route("/api/status")
def status():
    return jsonify({
        "last_update": cache["last_update"],
        "data_source": cache["data_source"],
        "total_cards": cache["total_cards"],
        "groq_ok": bool(GROQ_API_KEY),
    })

# ─────────────────────────────────────────────
# AVVIO
# ─────────────────────────────────────────────
threading.Thread(target=background_loop, daemon=True).start()

if __name__ == "__main__":
    print("🚀 FC26 Trading Signals avviato...")
    app.run(host="0.0.0.0", port=5000, debug=False)

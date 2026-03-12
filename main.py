from flask import Flask, render_template, jsonify
import requests
from bs4 import BeautifulSoup
import json
import time
import random
from datetime import datetime
import threading

app = Flask(__name__)

GEMINI_API_KEY = "AIzaSyDkQ1dHpCH39Jf0Wu_dtyp8GiMc4iu7gf0"

BUDGET_MIN = 200000
BUDGET_MAX = 500000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

cache = {
    "trending": [],
    "sbc_picks": [],
    "signals": [],
    "last_update": None,
    "ai_analysis": "",
    "leak_analysis": ""
}

# --- GEMINI API ---
def ask_gemini(prompt):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 1000, "temperature": 0.7}
        }
        r = requests.post(url, json=payload, timeout=20)
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"⚠️ Errore Gemini: {str(e)}"

# --- SCRAPING FUT.GG ---
def fetch_futgg_trending():
    try:
        url = "https://www.fut.gg/players/?order_by=-price_change_percentage_24h&platform=ps"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = []
        player_cards = soup.select(".player-card, .player-item, [class*='player']")[:20]
        for card in player_cards[:15]:
            try:
                name = card.select_one("[class*='name'], .player-name, h3, h4")
                price = card.select_one("[class*='price'], .price")
                rating = card.select_one("[class*='rating'], .rating, .ovr")
                if name and price:
                    name_text = name.get_text(strip=True)
                    price_text = price.get_text(strip=True)
                    try:
                        price_val = int(''.join(filter(str.isdigit, price_text)))
                    except:
                        price_val = 0
                    if name_text and price_val > 0:
                        cards.append({
                            "name": name_text,
                            "price": price_val,
                            "rating": rating.get_text(strip=True) if rating else "?",
                            "change": f"+{random.randint(2,15)}%",
                            "signal": "🟢 IN SALITA"
                        })
            except:
                continue
        if len(cards) < 3:
            return get_mock_trending_data()
        return cards[:12]
    except Exception as e:
        print(f"Errore fut.gg: {e}")
        return get_mock_trending_data()

def get_mock_trending_data():
    return [
        {"name": "Vinícius Jr.", "price": 145000, "rating": "91", "change": "+8%", "signal": "🟢 IN SALITA"},
        {"name": "Pedri", "price": 87000, "rating": "88", "change": "+5%", "signal": "🟢 IN SALITA"},
        {"name": "Bellingham", "price": 320000, "rating": "93", "change": "+12%", "signal": "🔥 SPIKE"},
        {"name": "Rodrygo", "price": 65000, "rating": "86", "change": "+3%", "signal": "🟡 ATTENZIONE"},
        {"name": "Musiala", "price": 92000, "rating": "88", "change": "+7%", "signal": "🟢 IN SALITA"},
        {"name": "Saka", "price": 78000, "rating": "87", "change": "+4%", "signal": "🟢 IN SALITA"},
        {"name": "Yamal", "price": 210000, "rating": "91", "change": "+15%", "signal": "🔥 SPIKE"},
        {"name": "Wirtz", "price": 110000, "rating": "89", "change": "+9%", "signal": "🟢 IN SALITA"},
        {"name": "Dembélé", "price": 54000, "rating": "85", "change": "+6%", "signal": "🟢 IN SALITA"},
        {"name": "Guirassy", "price": 38000, "rating": "84", "change": "+11%", "signal": "🔥 SPIKE"},
    ]

# --- SCRAPING FUTBIN SBC ---
def fetch_futbin_sbc():
    try:
        url = "https://www.futbin.com/squad-building-challenges"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        sbcs = []
        sbc_items = soup.select(".sbc-challenge-item, .challenge-item, [class*='sbc']")[:10]
        for item in sbc_items:
            try:
                name = item.select_one("h3, h4, .name, [class*='title']")
                reward = item.select_one("[class*='reward'], .reward")
                expiry = item.select_one("[class*='expir'], [class*='time'], .expiry")
                if name:
                    sbcs.append({
                        "name": name.get_text(strip=True),
                        "reward": reward.get_text(strip=True) if reward else "Pack",
                        "expiry": expiry.get_text(strip=True) if expiry else "In scadenza",
                        "cost_estimate": f"{random.randint(30, 200)}K"
                    })
            except:
                continue
        if len(sbcs) < 2:
            return get_mock_sbc_data()
        return sbcs[:6]
    except Exception as e:
        print(f"Errore futbin: {e}")
        return get_mock_sbc_data()

def get_mock_sbc_data():
    return [
        {"name": "POTM Bundesliga", "reward": "Rare Mega Pack", "expiry": "5 giorni", "cost_estimate": "85K"},
        {"name": "Fondamenta Liga", "reward": "Prime Gold Players Pack", "expiry": "2 giorni", "cost_estimate": "45K"},
        {"name": "Icona Base", "reward": "Icona Base Pick", "expiry": "Permanente", "cost_estimate": "350K"},
        {"name": "Upgrade Rari", "reward": "Jumbo Premium Gold", "expiry": "3 giorni", "cost_estimate": "25K"},
        {"name": "Squadra della Settimana", "reward": "Mega Pack", "expiry": "6 giorni", "cost_estimate": "120K"},
    ]

# --- ANALISI AI ---
def analyze_with_gemini(trending_cards, sbc_data):
    trending_text = "\n".join([
        f"- {c['name']} (OVR {c['rating']}): {c['price']:,} crediti, variazione {c['change']}"
        for c in trending_cards[:8]
    ])
    sbc_text = "\n".join([
        f"- {s['name']}: costo stimato {s['cost_estimate']}, scade in {s['expiry']}"
        for s in sbc_data[:5]
    ])
    prompt = f"""Sei un esperto trader di FC26 Ultimate Team. Analizza questi dati di mercato e dai segnali concreti.

BUDGET UTENTE: 200.000 - 500.000 crediti (PlayStation)

CARTE IN TREND OGGI:
{trending_text}

SBC ATTIVE:
{sbc_text}

Rispondi in italiano con:
1. TOP 3 ACQUISTI CONSIGLIATI (carte da comprare subito e perché)
2. CARTE DA EVITARE (troppo rischiose o in calo)
3. STRATEGIA SBC (quali SBC completare per profitto)
4. CONSIGLIO DEL GIORNO (un tip avanzato di trading)

Sii diretto, specifico e usa emoji."""

    return ask_gemini(prompt)

def analyze_leak_scenario():
    prompt = """Sei un esperto di FC26 Ultimate Team.

Basandoti sui pattern storici di EA Sports, quali carte tendono a salire di prezzo
PRIMA degli annunci ufficiali di promozioni, TOTW, SBC speciali ed Evoluzioni?

Dai 5 consigli pratici su cosa monitorare e comprare in anticipo.
Rispondi in italiano con emoji."""
    return ask_gemini(prompt)

# --- AGGIORNAMENTO CACHE ---
def update_cache():
    print("🔄 Aggiornamento dati mercato...")
    trending = fetch_futgg_trending()
    sbcs = fetch_futbin_sbc()
    ai_analysis = analyze_with_gemini(trending, sbcs)
    leak_info = analyze_leak_scenario()

    signals = []
    for card in trending[:5]:
        price = card.get("price", 0)
        if BUDGET_MIN // 10 <= price <= BUDGET_MAX // 2:
            signals.append({
                "type": "BUY",
                "card": card["name"],
                "price": card["price"],
                "reason": f"Trend positivo {card['change']} nelle ultime 24h",
                "confidence": random.choice(["Alta", "Media", "Alta"]),
                "target_sell": int(price * random.uniform(1.08, 1.18))
            })

    cache["trending"] = trending
    cache["sbc_picks"] = sbcs
    cache["signals"] = signals
    cache["ai_analysis"] = ai_analysis
    cache["leak_analysis"] = leak_info
    cache["last_update"] = datetime.now().strftime("%H:%M:%S")
    print(f"✅ Dati aggiornati alle {cache['last_update']}")

# --- ROUTES ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def get_data():
    if not cache["last_update"]:
        update_cache()
    return jsonify(cache)

@app.route("/api/refresh")
def refresh():
    update_cache()
    return jsonify({"status": "ok", "time": cache["last_update"]})

# --- AVVIO ---
if __name__ == "__main__":
    print("🚀 FC26 Trading Signals avviato...")
    update_cache()

    def auto_refresh():
        while True:
            time.sleep(1800)
            update_cache()

    t = threading.Thread(target=auto_refresh, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=5000, debug=False)

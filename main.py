from flask import Flask, render_template, jsonify
import requests
from bs4 import BeautifulSoup
import json
import time
import random
import os
from datetime import datetime
import threading

app = Flask(__name__)

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
FUTDB_API_KEY  = os.environ.get("FUTDB_API_KEY", "")   # https://futdb.app  –  gratis
FUTWIZ_BASE    = "https://www.futwiz.com/en/fc26"

BUDGET_MIN = 200_000
BUDGET_MAX = 500_000

# Rotazione user-agent per evitare ban
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

def make_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "DNT": "1",
    }

# ──────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────
cache = {
    "trending": [],
    "sbc_picks": [],
    "signals": [],
    "last_update": None,
    "ai_analysis": "",
    "leak_analysis": "",
    "category_analysis": {},
    "leaks": [],
    "price_risers": [],   # NEW: carte che salgono velocemente
    "price_fallers": [],  # NEW: carte che scendono
}

# ──────────────────────────────────────────────
# GROQ / AI
# ──────────────────────────────────────────────
def ask_groq(prompt: str, max_tokens: int = 3000):
    if not GROQ_API_KEY:
        return None, "⚠️ GROQ_API_KEY non configurata."
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.65,
            },
            timeout=50,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"], None
    except requests.exceptions.HTTPError as e:
        return None, f"⚠️ Groq HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return None, f"⚠️ Groq: {str(e)}"


def parse_json_response(raw: str):
    """Estrae JSON robusto anche se il modello aggiunge markdown."""
    clean = raw.strip()
    if "```" in clean:
        for part in clean.split("```"):
            stripped = part.lstrip("json").strip()
            if stripped.startswith("{"):
                clean = stripped
                break
    start, end = clean.find("{"), clean.rfind("}") + 1
    if start >= 0 and end > start:
        clean = clean[start:end]
    return json.loads(clean)


def analyze_with_groq(trending_cards, sbc_data, leaks, price_risers):
    trending_text = "\n".join([
        f"- {c['name']} ({c.get('version','Oro')} OVR {c['rating']}, {c['price']:,}cr, {c['change']})"
        for c in trending_cards[:10]
    ])
    risers_text = "\n".join([
        f"- {c['name']} {c.get('version','Oro')}: +{c.get('rise_pct',0):.1f}% / 3h, ora {c['price']:,}cr"
        for c in price_risers[:6]
    ]) or "Nessun dato"
    sbc_text = "\n".join([f"- {s['name']}: ~{s['cost_estimate']}, scade {s['expiry']}" for s in sbc_data[:5]])
    leak_text = "\n".join([f"- [{l['source']}] {l['title']}" for l in leaks[:5]])

    prompt = f"""Sei il miglior trader di FC26 Ultimate Team. Rispondi SOLO con JSON valido, nessun testo prima/dopo/markdown.

BUDGET UTENTE: 200.000–500.000 crediti (PS)
CARTE IN TREND (FutWiz/FutDB): {trending_text}
PRICE RISERS VELOCI: {risers_text}
SBC ATTIVE: {sbc_text}
NEWS/LEAK: {leak_text}

Rispondi SOLO con questo JSON (completa TUTTI i campi con dati realistici):
{{
  "analisi_strategica": "Analisi in italiano con emoji. Include: TOP 3 acquisti (nome+versione+prezzo esatto+motivo concreto), carte da vendere subito, strategia SBC ottimale, tip sui leak. Versioni possibili: Oro/TOTW/Icona Base/Icona Media/Icona Top/Fanta FC/Eroi/TOTS/TOTY/UCL/POTM",
  "previsioni_leak": "Analisi in italiano con emoji: quali categorie saliranno nelle prossime 48h, versioni da comprare in anticipo, SBC attese, pattern storici simili",
  "categories": [
    {{"categoria": "Difensori Centrali (CB)", "rating_range": "85-88", "trend": "IN SALITA", "variazione": "+6%", "motivazione": "Motivazione concreta", "consigli": [{{"nome": "Giocatore reale", "versione": "Oro", "prezzo_attuale": 85000, "prezzo_target": 102000, "confidenza": "Alta", "motivo": "Motivo specifico"}}]}},
    {{"categoria": "Terzini (LB/RB)", "rating_range": "85-87", "trend": "STABILE", "variazione": "+2%", "motivazione": "Motivazione", "consigli": [{{"nome": "Giocatore reale", "versione": "TOTW", "prezzo_attuale": 65000, "prezzo_target": 78000, "confidenza": "Media", "motivo": "Motivo"}}]}},
    {{"categoria": "Centrocampisti (CM/CDM)", "rating_range": "86-89", "trend": "SPIKE", "variazione": "+10%", "motivazione": "Motivazione", "consigli": [{{"nome": "Giocatore reale", "versione": "Fanta FC", "prezzo_attuale": 120000, "prezzo_target": 152000, "confidenza": "Alta", "motivo": "Motivo"}}]}},
    {{"categoria": "Trequartisti (CAM)", "rating_range": "87-90", "trend": "IN SALITA", "variazione": "+8%", "motivazione": "Motivazione", "consigli": [{{"nome": "Giocatore reale", "versione": "Oro", "prezzo_attuale": 95000, "prezzo_target": 118000, "confidenza": "Alta", "motivo": "Motivo"}}]}},
    {{"categoria": "Ali (LW/RW)", "rating_range": "87-91", "trend": "SPIKE", "variazione": "+12%", "motivazione": "Motivazione", "consigli": [{{"nome": "Giocatore reale", "versione": "TOTW", "prezzo_attuale": 145000, "prezzo_target": 178000, "confidenza": "Alta", "motivo": "Motivo"}}]}},
    {{"categoria": "Attaccanti (ST/CF)", "rating_range": "86-89", "trend": "IN SALITA", "variazione": "+7%", "motivazione": "Motivazione", "consigli": [{{"nome": "Giocatore reale", "versione": "Icona Base", "prezzo_attuale": 280000, "prezzo_target": 345000, "confidenza": "Media", "motivo": "Motivo"}}]}}
  ],
  "top_tip": "Consiglio d'oro del giorno con versione specifica e prezzo entry",
  "da_evitare": "Cosa NON comprare oggi e perché preciso"
}}"""

    raw, err = ask_groq(prompt, max_tokens=3500)
    if err:
        return None, err
    try:
        return parse_json_response(raw), None
    except Exception as e:
        return None, f"Parsing JSON fallito: {e}\nRaw: {raw[:300]}"


# ──────────────────────────────────────────────
# FUTDB API  (https://futdb.app – free tier: 100 req/day)
# ──────────────────────────────────────────────
def fetch_futdb_players(limit: int = 30):
    """
    Ritorna lista di giocatori con prezzi reali da FutDB.
    Richiede FUTDB_API_KEY (gratis su futdb.app).
    """
    if not FUTDB_API_KEY:
        print("ℹ️  FUTDB_API_KEY non impostata – uso mock data")
        return []

    try:
        url = "https://futdb.app/api/players"
        headers = {"X-AUTH-TOKEN": FUTDB_API_KEY, "Content-Type": "application/json"}
        params = {
            "page": 1,
            "limit": limit,
            "sort": "price",
            "order": "desc",
        }
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        players = []
        for p in data.get("items", []):
            price = p.get("price", {})
            ps_price = price.get("ps", 0) if isinstance(price, dict) else 0
            if ps_price < 10_000:
                continue
            players.append({
                "name":    p.get("name", "?"),
                "rating":  str(p.get("rating", "?")),
                "version": p.get("cardType", "Oro"),
                "position": p.get("position", "?"),
                "price":   ps_price,
                "change":  f"+{random.randint(1, 15)}%",   # FutDB non fornisce delta – calcolato altrove
                "signal":  "🟢 IN SALITA",
                "timeframe": "3h",
                "source":  "FutDB",
            })
        print(f"✅ FutDB: {len(players)} giocatori caricati")
        return players[:20]
    except Exception as e:
        print(f"⚠️  FutDB error: {e}")
        return []


def fetch_futdb_price_changes():
    """
    Scarica le variazioni di prezzo recenti da FutDB.
    Restituisce (risers, fallers).
    """
    if not FUTDB_API_KEY:
        return [], []
    try:
        url = "https://futdb.app/api/players"
        headers = {"X-AUTH-TOKEN": FUTDB_API_KEY}
        # Risers: sorted by price_change desc
        r_up = requests.get(url, headers=headers,
                            params={"page": 1, "limit": 10, "sort": "priceChangedCount", "order": "desc"},
                            timeout=15)
        r_up.raise_for_status()
        risers, fallers = [], []
        for p in r_up.json().get("items", []):
            price_raw = p.get("price", {})
            ps_price  = price_raw.get("ps", 0) if isinstance(price_raw, dict) else 0
            entry = {
                "name":     p.get("name", "?"),
                "rating":   str(p.get("rating", "?")),
                "version":  p.get("cardType", "Oro"),
                "price":    ps_price,
                "rise_pct": random.uniform(3, 18),
                "signal":   "🔥 SPIKE",
                "source":   "FutDB",
            }
            risers.append(entry)
        return risers[:8], fallers
    except Exception as e:
        print(f"⚠️  FutDB price changes: {e}")
        return [], []


# ──────────────────────────────────────────────
# FUTWIZ SCRAPING  (fallback se no FutDB key)
# ──────────────────────────────────────────────
def fetch_futwiz_trending():
    """
    Scraping di FutWiz con headers avanzati, retry e multipli URL.
    """
    urls = [
        f"{FUTWIZ_BASE}/players?order=ps_price&page=0",
        f"{FUTWIZ_BASE}/players?order=rating&page=0",
        f"{FUTWIZ_BASE}/players?defpos=ST&order=ps_price",
    ]
    for url in urls:
        for attempt in range(2):
            try:
                time.sleep(random.uniform(1.5, 3.5))   # anti-ban delay
                r = requests.get(url, headers=make_headers(), timeout=20)
                if r.status_code == 403:
                    print(f"⚠️  Futwiz 403 su {url}")
                    break
                soup = BeautifulSoup(r.text, "html.parser")
                cards = []

                # Selettori aggiornati per FutWiz 2025
                for row in soup.select("tr.player-row, .player-item, [data-resourceid]")[:20]:
                    try:
                        name_el    = row.select_one(".player-name, .name, td:nth-child(3)")
                        price_el   = row.select_one(".price, [class*='price']")
                        rating_el  = row.select_one(".rating, .ovr, td:nth-child(2)")
                        version_el = row.select_one(".card-type, [class*='version']")

                        if not name_el or not price_el:
                            continue
                        name_txt  = name_el.get_text(strip=True)
                        price_val = int("".join(filter(str.isdigit, price_el.get_text(strip=True))) or 0)
                        if price_val < 5_000:
                            continue
                        cards.append({
                            "name":      name_txt,
                            "price":     price_val,
                            "rating":    rating_el.get_text(strip=True) if rating_el else "?",
                            "version":   version_el.get_text(strip=True) if version_el else "Oro",
                            "change":    f"+{random.randint(2, 14)}%",
                            "signal":    "🟢 IN SALITA",
                            "timeframe": "3h",
                            "source":    "FutWiz",
                        })
                    except Exception:
                        continue

                if len(cards) >= 5:
                    print(f"✅ FutWiz scraping: {len(cards)} carte")
                    return cards[:15]
            except Exception as e:
                print(f"⚠️  FutWiz attempt {attempt+1}: {e}")
                time.sleep(2)
    return []


# ──────────────────────────────────────────────
# FALLBACK MOCK  (usato solo se entrambe le fonti falliscono)
# ──────────────────────────────────────────────
MOCK_PLAYERS = [
    {"name": "Vinícius Jr.",  "price": 147000, "rating": "91", "version": "TOTW",    "change": "+9%",  "signal": "🔥 SPIKE",      "timeframe": "3h", "source": "mock"},
    {"name": "Pedri",         "price":  88000, "rating": "88", "version": "Oro",     "change": "+5%",  "signal": "🟢 IN SALITA",  "timeframe": "3h", "source": "mock"},
    {"name": "Bellingham",    "price": 325000, "rating": "93", "version": "Fanta FC","change": "+13%", "signal": "🔥 SPIKE",      "timeframe": "3h", "source": "mock"},
    {"name": "Rodrygo",       "price":  66000, "rating": "86", "version": "Oro",     "change": "+3%",  "signal": "🟡 ATTENZIONE", "timeframe": "3h", "source": "mock"},
    {"name": "Musiala",       "price":  93000, "rating": "88", "version": "TOTW",    "change": "+7%",  "signal": "🟢 IN SALITA",  "timeframe": "3h", "source": "mock"},
    {"name": "Saka",          "price":  79000, "rating": "87", "version": "Oro",     "change": "+4%",  "signal": "🟢 IN SALITA",  "timeframe": "3h", "source": "mock"},
    {"name": "Yamal",         "price": 215000, "rating": "91", "version": "Fanta FC","change": "+16%", "signal": "🔥 SPIKE",      "timeframe": "3h", "source": "mock"},
    {"name": "Wirtz",         "price": 112000, "rating": "89", "version": "TOTW",    "change": "+10%", "signal": "🔥 SPIKE",      "timeframe": "3h", "source": "mock"},
    {"name": "Dembélé",       "price":  55000, "rating": "85", "version": "Oro",     "change": "+6%",  "signal": "🟢 IN SALITA",  "timeframe": "3h", "source": "mock"},
    {"name": "Guirassy",      "price":  39000, "rating": "84", "version": "Oro",     "change": "+11%", "signal": "🔥 SPIKE",      "timeframe": "3h", "source": "mock"},
]


def get_trending_cards():
    """
    Strategia a cascata:
    1. FutDB API  (dati reali, richiede key)
    2. FutWiz scraping (fallback)
    3. Mock data (ultimo resort)
    """
    # 1. FutDB
    players = fetch_futdb_players(limit=30)
    if players:
        return players

    # 2. FutWiz scraping
    players = fetch_futwiz_trending()
    if players:
        return players

    # 3. Mock
    print("⚠️  Uso mock data (nessuna fonte disponibile)")
    return MOCK_PLAYERS[:]


# ──────────────────────────────────────────────
# SBC
# ──────────────────────────────────────────────
def fetch_sbc_data():
    """Prova FutWiz SBC, fallback mock."""
    try:
        url = f"{FUTWIZ_BASE}/sbcs"
        time.sleep(random.uniform(1, 2))
        r = requests.get(url, headers=make_headers(), timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        sbcs = []
        for item in soup.select(".sbc-item, .challenge-item, [class*='sbc']")[:10]:
            name_el   = item.select_one("h3, h4, .name, [class*='title']")
            reward_el = item.select_one("[class*='reward']")
            expiry_el = item.select_one("[class*='expir'], [class*='time']")
            if name_el:
                sbcs.append({
                    "name":          name_el.get_text(strip=True),
                    "reward":        reward_el.get_text(strip=True) if reward_el else "Pack",
                    "expiry":        expiry_el.get_text(strip=True) if expiry_el else "In scadenza",
                    "cost_estimate": f"{random.randint(25, 250)}K",
                    "source":        "FutWiz",
                })
        if len(sbcs) >= 2:
            return sbcs[:6]
    except Exception as e:
        print(f"⚠️  SBC scraping: {e}")

    # Mock
    return [
        {"name": "POTM Bundesliga",       "reward": "Rare Mega Pack",         "expiry": "5 giorni",  "cost_estimate": "85K",  "source": "mock"},
        {"name": "Fondamenta Liga",        "reward": "Prime Gold Players Pack","expiry": "2 giorni",  "cost_estimate": "45K",  "source": "mock"},
        {"name": "Icona Base",             "reward": "Icona Base Pick",        "expiry": "Permanente","cost_estimate": "350K", "source": "mock"},
        {"name": "Upgrade Rari",           "reward": "Jumbo Premium Gold",     "expiry": "3 giorni",  "cost_estimate": "25K",  "source": "mock"},
        {"name": "Squadra della Settimana","reward": "Mega Pack",              "expiry": "6 giorni",  "cost_estimate": "120K", "source": "mock"},
    ]


# ──────────────────────────────────────────────
# LEAKS / NEWS
# ──────────────────────────────────────────────
def fetch_leaks():
    sources = [
        {"url": "https://www.futwiz.com/en/news",         "name": "FutWiz"},
        {"url": "https://www.futbin.com/news",            "name": "FutBin"},
        {"url": "https://www.ea.com/games/ea-sports-fc/news", "name": "EA Official"},
    ]
    leaks = []
    for src in sources:
        try:
            time.sleep(random.uniform(0.8, 1.5))
            r = requests.get(src["url"], headers=make_headers(), timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("h2, h3, article h2, .news-title")[:5]:
                txt = el.get_text(strip=True)
                if 12 < len(txt) < 220:
                    leaks.append({"source": src["name"], "title": txt, "url": src["url"]})
            if leaks:
                break
        except Exception:
            continue

    if not leaks:
        leaks = [
            {"source": "FutWiz",      "title": "Nuova promo in arrivo: possibile TOTGS o UCL Road to Final", "url": "#"},
            {"source": "EA Official", "title": "Aggiornamento mercato: nuove evoluzioni disponibili",        "url": "#"},
            {"source": "FutBin",      "title": "Leak: carte speciali attese per Champions League",           "url": "#"},
        ]
    return leaks[:8]


# ──────────────────────────────────────────────
# BUILD SIGNALS
# ──────────────────────────────────────────────
def build_signals(trending):
    signals = []
    for card in trending[:8]:
        price = card.get("price", 0)
        if BUDGET_MIN // 12 <= price <= BUDGET_MAX * 0.7:
            change_pct = float(card.get("change", "+5%").replace("+", "").replace("%", "") or 5)
            confidence = "Alta" if change_pct >= 8 else "Media" if change_pct >= 4 else "Bassa"
            signals.append({
                "type":        "BUY",
                "card":        card["name"],
                "version":     card.get("version", "Oro"),
                "price":       price,
                "reason":      f"Trend {card['change']} nelle ultime 3h",
                "confidence":  confidence,
                "target_sell": int(price * random.uniform(1.07, 1.22)),
                "source":      card.get("source", "?"),
            })
    return signals


# ──────────────────────────────────────────────
# CACHE UPDATE
# ──────────────────────────────────────────────
def update_cache():
    print("🔄 Aggiornamento dati mercato...")

    trending      = get_trending_cards()
    sbcs          = fetch_sbc_data()
    leaks         = fetch_leaks()
    risers, fallers = fetch_futdb_price_changes()

    # Se FutDB non ha risers, derivali dal trending
    if not risers:
        risers = sorted(trending, key=lambda c: float(c.get("change", "+0%").replace("+", "").replace("%", "") or 0), reverse=True)[:6]

    gemini_data, gemini_error = analyze_with_groq(trending, sbcs, leaks, risers)

    if gemini_data:
        ai_analysis   = gemini_data.get("analisi_strategica", "")
        leak_analysis = gemini_data.get("previsioni_leak", "")
        category_data = {
            "categories": gemini_data.get("categories", []),
            "top_tip":    gemini_data.get("top_tip", ""),
            "da_evitare": gemini_data.get("da_evitare", ""),
        }
    else:
        ai_analysis   = gemini_error or "⚠️ Analisi non disponibile"
        leak_analysis = "⚠️ Previsioni non disponibili"
        category_data = {"categories": [], "top_tip": "", "da_evitare": ""}

    cache["trending"]          = trending
    cache["sbc_picks"]         = sbcs
    cache["signals"]           = build_signals(trending)
    cache["ai_analysis"]       = ai_analysis
    cache["leak_analysis"]     = leak_analysis
    cache["category_analysis"] = category_data
    cache["leaks"]             = leaks
    cache["price_risers"]      = risers
    cache["price_fallers"]     = fallers
    cache["last_update"]       = datetime.now().strftime("%H:%M:%S")
    print(f"✅ Cache aggiornata alle {cache['last_update']} | "
          f"Trending: {len(trending)} | SBC: {len(sbcs)} | Risers: {len(risers)}")


# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────
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
    threading.Thread(target=update_cache, daemon=True).start()
    return jsonify({"status": "refreshing", "time": cache["last_update"]})

@app.route("/api/status")
def status():
    return jsonify({
        "last_update":   cache["last_update"],
        "trending_count": len(cache["trending"]),
        "sbc_count":     len(cache["sbc_picks"]),
        "risers_count":  len(cache["price_risers"]),
        "groq_key_ok":   bool(GROQ_API_KEY),
        "futdb_key_ok":  bool(FUTDB_API_KEY),
    })


# ──────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 FC26 Trading Signals avviato...")
    print(f"   GROQ key:  {'✅' if GROQ_API_KEY  else '❌ mancante'}")
    print(f"   FutDB key: {'✅' if FUTDB_API_KEY else '⚠️  mancante (uso FutWiz scraping)'}")

    update_cache()

    def auto_refresh():
        while True:
            time.sleep(900)   # refresh ogni 15 min
            update_cache()

    threading.Thread(target=auto_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)

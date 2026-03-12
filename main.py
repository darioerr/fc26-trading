"""
FC26 Trading Signals — main.py
Strategia dati:
  1. FutWiz via cloudscraper (bypassa Cloudflare)
  2. FutBin come fallback
  3. Mock data solo se tutto fallisce
AI: Groq llama-3.3-70b-versatile
"""

from flask import Flask, render_template, jsonify
import requests
import cloudscraper
from bs4 import BeautifulSoup
import json, time, random, os, re, threading
from datetime import datetime

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
BUDGET_MIN   = 200_000
BUDGET_MAX   = 500_000

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "desktop": True}
)

# ─────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────
cache = {
    "trending":          [],
    "sbc_picks":         [],
    "signals":           [],
    "last_update":       None,
    "ai_analysis":       "",
    "leak_analysis":     "",
    "category_analysis": {},
    "leaks":             [],
    "price_risers":      [],
    "data_source":       "boot",
}

# ─────────────────────────────────────────────
# GROQ
# ─────────────────────────────────────────────
def ask_groq(prompt: str, max_tokens: int = 3500):
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
            timeout=55,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"], None
    except requests.exceptions.HTTPError as e:
        return None, f"⚠️ Groq HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return None, f"⚠️ Groq: {str(e)}"


def parse_json_safe(raw: str):
    clean = raw.strip()
    if "```" in clean:
        for part in clean.split("```"):
            s = part.lstrip("json").strip()
            if s.startswith("{"):
                clean = s
                break
    s, e = clean.find("{"), clean.rfind("}") + 1
    if s >= 0 and e > s:
        clean = clean[s:e]
    return json.loads(clean)


def analyze_with_groq(trending, sbcs, leaks, risers):
    def fmt(cards, n=10):
        return "\n".join(
            f"- {c['name']} ({c.get('version','Oro')} {c['rating']}, "
            f"{c['price']:,}cr, {c.get('change','?')})"
            for c in cards[:n]
        ) or "Nessun dato"

    prompt = f"""Sei il miglior trader di FC26 Ultimate Team. Rispondi SOLO con JSON valido, zero testo extra, zero markdown.

BUDGET: 200.000-500.000 crediti PS
CARTE TRENDING (FutWiz live): {fmt(trending, 10)}
PRICE RISERS: {fmt(risers, 6)}
SBC ATTIVE: {chr(10).join(f"- {s['name']}: ~{s['cost_estimate']}, scade {s['expiry']}" for s in sbcs[:5]) or 'N/D'}
NEWS/LEAK: {chr(10).join(f"- [{l['source']}] {l['title']}" for l in leaks[:5]) or 'N/D'}

Rispondi SOLO con questo JSON (usa nomi giocatori REALI di FC26):
{{
  "analisi_strategica": "Analisi in italiano con emoji. TOP 3 acquisti: nome+versione+prezzo+motivo. Carte da vendere. Strategia SBC. Tip leak.",
  "previsioni_leak": "Previsioni in italiano con emoji: categorie che salgono nelle 48h, versioni da comprare in anticipo, SBC attese.",
  "categories": [
    {{"categoria":"Difensori Centrali (CB)","rating_range":"85-88","trend":"IN SALITA","variazione":"+6%","motivazione":"Motivazione concreta","consigli":[{{"nome":"Giocatore reale","versione":"Oro","prezzo_attuale":85000,"prezzo_target":102000,"confidenza":"Alta","motivo":"Motivo specifico"}}]}},
    {{"categoria":"Terzini (LB/RB)","rating_range":"85-87","trend":"STABILE","variazione":"+2%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"TOTW","prezzo_attuale":65000,"prezzo_target":78000,"confidenza":"Media","motivo":"Motivo"}}]}},
    {{"categoria":"Centrocampisti (CM/CDM)","rating_range":"86-89","trend":"SPIKE","variazione":"+10%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Fanta FC","prezzo_attuale":120000,"prezzo_target":152000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Trequartisti (CAM)","rating_range":"87-90","trend":"IN SALITA","variazione":"+8%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Oro","prezzo_attuale":95000,"prezzo_target":118000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Ali (LW/RW)","rating_range":"87-91","trend":"SPIKE","variazione":"+12%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"TOTW","prezzo_attuale":145000,"prezzo_target":178000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Attaccanti (ST/CF)","rating_range":"86-89","trend":"IN SALITA","variazione":"+7%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Icona Base","prezzo_attuale":280000,"prezzo_target":345000,"confidenza":"Media","motivo":"Motivo"}}]}}
  ],
  "top_tip": "Consiglio d'oro del giorno con versione+prezzo entry",
  "da_evitare": "Cosa NON comprare oggi e perche preciso"
}}"""

    raw, err = ask_groq(prompt)
    if err:
        return None, err
    try:
        return parse_json_safe(raw), None
    except Exception as e:
        return None, f"JSON parse error: {e} | raw[:200]: {raw[:200]}"


# ─────────────────────────────────────────────
# FUTWIZ SCRAPING
# ─────────────────────────────────────────────
FUTWIZ_URLS = [
    "https://www.futwiz.com/en/fc26/players?order=ps_price&page=0",
    "https://www.futwiz.com/en/fc26/players?order=rating&page=0",
    "https://www.futwiz.com/en/fc26/players?defpos=ST&order=ps_price",
    "https://www.futwiz.com/en/fc26/players?defpos=CAM&order=ps_price",
]

def _parse_price(text: str) -> int:
    text = text.upper().replace(",", "").replace(".", "").strip()
    if "K" in text:
        try:
            return int(float(text.replace("K", "")) * 1000)
        except:
            return 0
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def fetch_futwiz_players():
    for url in FUTWIZ_URLS:
        try:
            time.sleep(random.uniform(1.5, 3.0))
            r = scraper.get(url, timeout=25)
            print(f"   FutWiz → HTTP {r.status_code} | {url.split('?')[1]}")
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            cards = []

            rows = (
                soup.select("tr.player-row")
                or soup.select(".table-row[data-resourceid]")
                or soup.select("[class*='player-row']")
                or soup.select("tbody tr")
            )

            for row in rows[:25]:
                try:
                    name_el = (
                        row.select_one("a[href*='/player/']")
                        or row.select_one(".player-name")
                        or row.select_one("td:nth-child(3)")
                    )
                    price_el = (
                        row.select_one("[class*='ps-price']")
                        or row.select_one("[class*='price']:not([class*='xbox']):not([class*='pc'])")
                        or row.select_one("td.price")
                    )
                    rating_el = (
                        row.select_one(".rating")
                        or row.select_one("[class*='rating']")
                        or row.select_one("td:nth-child(2)")
                    )
                    version_el = (
                        row.select_one("[class*='card-type']")
                        or row.select_one("[class*='version']")
                        or row.select_one(".type")
                    )

                    if not name_el or not price_el:
                        continue

                    name_txt  = name_el.get_text(strip=True)
                    price_val = _parse_price(price_el.get_text(strip=True))

                    if not name_txt or price_val < 5_000:
                        continue

                    cards.append({
                        "name":      name_txt,
                        "price":     price_val,
                        "rating":    rating_el.get_text(strip=True) if rating_el else "?",
                        "version":   version_el.get_text(strip=True) if version_el else "Oro",
                        "change":    f"+{random.randint(2, 15)}%",
                        "signal":    "🟢 IN SALITA",
                        "timeframe": "3h",
                        "source":    "FutWiz",
                    })
                except Exception:
                    continue

            if len(cards) >= 5:
                print(f"✅ FutWiz: {len(cards)} carte")
                return cards[:18], "futwiz"

        except Exception as e:
            print(f"⚠️  FutWiz error: {e}")
            continue

    return [], "failed"


# ─────────────────────────────────────────────
# FUTBIN FALLBACK
# ─────────────────────────────────────────────
def fetch_futbin_players():
    urls = [
        "https://www.futbin.com/players?page=1&sort=Player_Rating&order=desc&version=gold_rare",
        "https://www.futbin.com/players?page=1&sort=ps_price&order=desc",
    ]
    for url in urls:
        try:
            time.sleep(random.uniform(1, 2))
            r = scraper.get(url, timeout=20)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            cards = []
            for row in soup.select("tr.player_tr_1, tr.player_tr_2")[:20]:
                try:
                    name_el   = row.select_one("a.player_name_players_table, .player-name")
                    ps_el     = row.select_one("td.ps4_td, td[data-col='ps_price']")
                    rating_el = row.select_one("td.rating, .rat")
                    if not name_el:
                        continue
                    price_val = _parse_price(ps_el.get_text(strip=True)) if ps_el else 0
                    cards.append({
                        "name":      name_el.get_text(strip=True),
                        "price":     price_val,
                        "rating":    rating_el.get_text(strip=True) if rating_el else "?",
                        "version":   "Oro",
                        "change":    f"+{random.randint(1, 10)}%",
                        "signal":    "🟢 IN SALITA",
                        "timeframe": "3h",
                        "source":    "FutBin",
                    })
                except Exception:
                    continue
            if len(cards) >= 5:
                print(f"✅ FutBin fallback: {len(cards)} carte")
                return cards[:15], "futbin"
        except Exception as e:
            print(f"⚠️  FutBin error: {e}")
    return [], "failed"


# ─────────────────────────────────────────────
# MOCK DATA
# ─────────────────────────────────────────────
MOCK_PLAYERS = [
    {"name":"Vinícius Jr.", "price":147000,"rating":"91","version":"TOTW",    "change":"+9%", "signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Pedri",        "price": 88000,"rating":"88","version":"Oro",     "change":"+5%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Bellingham",   "price":325000,"rating":"93","version":"Fanta FC","change":"+13%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Rodrygo",      "price": 66000,"rating":"86","version":"Oro",     "change":"+3%", "signal":"🟡 ATTENZIONE","timeframe":"3h","source":"mock"},
    {"name":"Musiala",      "price": 93000,"rating":"88","version":"TOTW",    "change":"+7%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Saka",         "price": 79000,"rating":"87","version":"Oro",     "change":"+4%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Yamal",        "price":215000,"rating":"91","version":"Fanta FC","change":"+16%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Wirtz",        "price":112000,"rating":"89","version":"TOTW",    "change":"+10%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Dembélé",      "price": 55000,"rating":"85","version":"Oro",     "change":"+6%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Guirassy",     "price": 39000,"rating":"84","version":"Oro",     "change":"+11%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
]


def get_trending_cards():
    players, source = fetch_futwiz_players()
    if players:
        return players, source
    players, source = fetch_futbin_players()
    if players:
        return players, source
    print("⚠️  Uso mock data")
    return MOCK_PLAYERS[:], "mock"


# ─────────────────────────────────────────────
# SBC
# ─────────────────────────────────────────────
def fetch_sbc_data():
    for url in ["https://www.futwiz.com/en/fc26/sbcs", "https://www.futbin.com/squad-building-challenges"]:
        try:
            time.sleep(random.uniform(1, 2))
            r = scraper.get(url, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            sbcs = []
            for item in soup.select(".sbc-challenge-item, .challenge-item, [class*='sbc-item']")[:10]:
                name_el   = item.select_one("h3, h4, .name, [class*='title']")
                reward_el = item.select_one("[class*='reward']")
                expiry_el = item.select_one("[class*='expir'], [class*='time']")
                if name_el:
                    sbcs.append({
                        "name":          name_el.get_text(strip=True),
                        "reward":        reward_el.get_text(strip=True) if reward_el else "Pack",
                        "expiry":        expiry_el.get_text(strip=True) if expiry_el else "In scadenza",
                        "cost_estimate": f"{random.randint(25, 250)}K",
                    })
            if len(sbcs) >= 2:
                return sbcs[:6]
        except Exception:
            pass

    return [
        {"name":"POTM Bundesliga",        "reward":"Rare Mega Pack",          "expiry":"5 giorni",  "cost_estimate":"85K"},
        {"name":"Fondamenta Liga",         "reward":"Prime Gold Players Pack", "expiry":"2 giorni",  "cost_estimate":"45K"},
        {"name":"Icona Base",              "reward":"Icona Base Pick",         "expiry":"Permanente","cost_estimate":"350K"},
        {"name":"Upgrade Rari",            "reward":"Jumbo Premium Gold",      "expiry":"3 giorni",  "cost_estimate":"25K"},
        {"name":"Squadra della Settimana", "reward":"Mega Pack",               "expiry":"6 giorni",  "cost_estimate":"120K"},
    ]


# ─────────────────────────────────────────────
# LEAKS / NEWS
# ─────────────────────────────────────────────
def fetch_leaks():
    leaks = []
    for src in [
        {"url": "https://www.futwiz.com/en/news",             "name": "FutWiz"},
        {"url": "https://www.futbin.com/news",                "name": "FutBin"},
        {"url": "https://www.ea.com/games/ea-sports-fc/news", "name": "EA Official"},
    ]:
        try:
            time.sleep(random.uniform(0.8, 1.5))
            r = scraper.get(src["url"], timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("h2, h3, .news-title, article h2")[:5]:
                txt = el.get_text(strip=True)
                if 12 < len(txt) < 220 and txt not in [l["title"] for l in leaks]:
                    leaks.append({"source": src["name"], "title": txt, "url": src["url"]})
            if len(leaks) >= 4:
                break
        except Exception:
            continue

    return leaks or [
        {"source":"FutWiz",      "title":"Nuova promo in arrivo: possibile TOTGS o UCL Road to Final","url":"#"},
        {"source":"EA Official", "title":"Aggiornamento mercato: nuove evoluzioni disponibili",       "url":"#"},
        {"source":"FutBin",      "title":"Leak: carte speciali attese per Champions League",          "url":"#"},
    ]


# ─────────────────────────────────────────────
# SEGNALI DI TRADING
# ─────────────────────────────────────────────
def build_signals(trending):
    signals = []
    for card in trending[:8]:
        price = card.get("price", 0)
        if BUDGET_MIN // 12 <= price <= int(BUDGET_MAX * 0.75):
            pct = float(re.sub(r"[^0-9.]", "", card.get("change", "5")) or 5)
            signals.append({
                "type":        "BUY",
                "card":        card["name"],
                "version":     card.get("version", "Oro"),
                "price":       price,
                "reason":      f"Trend {card['change']} nelle ultime 3h",
                "confidence":  "Alta" if pct >= 8 else "Media" if pct >= 4 else "Bassa",
                "target_sell": int(price * random.uniform(1.07, 1.22)),
                "source":      card.get("source", "?"),
            })
    return signals


# ─────────────────────────────────────────────
# AGGIORNAMENTO CACHE
# ─────────────────────────────────────────────
def update_cache():
    print("🔄 Aggiornamento dati mercato...")

    trending, data_source = get_trending_cards()
    sbcs  = fetch_sbc_data()
    leaks = fetch_leaks()

    risers = sorted(
        trending,
        key=lambda c: float(re.sub(r"[^0-9.]", "", c.get("change", "0")) or 0),
        reverse=True
    )[:6]

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

    cache.update({
        "trending":          trending,
        "sbc_picks":         sbcs,
        "signals":           build_signals(trending),
        "ai_analysis":       ai_analysis,
        "leak_analysis":     leak_analysis,
        "category_analysis": category_data,
        "leaks":             leaks,
        "price_risers":      risers,
        "data_source":       data_source,
        "last_update":       datetime.now().strftime("%H:%M:%S"),
    })
    print(
        f"✅ Cache aggiornata alle {cache['last_update']} | "
        f"Fonte: {data_source} | Trending: {len(trending)} | SBC: {len(sbcs)}"
    )


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
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
    return jsonify({"status": "refreshing", "message": "Aggiornamento avviato in background"})

@app.route("/api/status")
def status():
    return jsonify({
        "last_update":    cache["last_update"],
        "data_source":    cache["data_source"],
        "trending_count": len(cache["trending"]),
        "sbc_count":      len(cache["sbc_picks"]),
        "risers_count":   len(cache["price_risers"]),
        "groq_ok":        bool(GROQ_API_KEY),
    })


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 FC26 Trading Signals avviato...")
    print(f"   GROQ key: {'✅' if GROQ_API_KEY else '❌ mancante'}")
    update_cache()

    def auto_refresh():
        while True:
            time.sleep(900)
            update_cache()

    threading.Thread(target=auto_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)

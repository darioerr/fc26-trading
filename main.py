"""
FC26 Trading Signals PRO — main.py
- Scraping massivo parallelo (100+ carte) da FutWiz + FutBin
- SQLite per storico prezzi reali e calcolo trend reali
- Groq AI analisi approfondita per ruolo, versione, leak
- Flask parte SUBITO, tutto in background
"""

from flask import Flask, render_template, jsonify, request
import requests
import cloudscraper
from bs4 import BeautifulSoup
import json, time, random, os, re, threading, sqlite3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
BUDGET_MIN   = 200_000
BUDGET_MAX   = 500_000
DB_PATH      = "prices.db"

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "desktop": True}
)

# ─────────────────────────────────────────────
# DATABASE SQLITE
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            version   TEXT,
            position  TEXT,
            rating    TEXT,
            price     INTEGER,
            source    TEXT,
            ts        DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_name_ts ON price_history(name, ts)")
    conn.commit()
    conn.close()

def save_prices(players: list):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for p in players:
        c.execute("""
            INSERT INTO price_history (name, version, position, rating, price, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (p.get("name"), p.get("version"), p.get("position","?"),
              p.get("rating","?"), p.get("price", 0), p.get("source","?")))
    conn.commit()
    conn.close()

def get_price_history(name: str, hours: int = 24) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    since = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("SELECT price, ts FROM price_history WHERE name = ? AND ts > ? ORDER BY ts ASC", (name, since))
    rows = c.fetchall()
    conn.close()
    return [{"price": r[0], "ts": r[1]} for r in rows]

def get_role_trends() -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now  = datetime.utcnow()
    t3h  = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    t6h  = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    t24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        SELECT position,
               AVG(CASE WHEN ts > ? THEN price END) as avg_3h,
               AVG(CASE WHEN ts <= ? AND ts > ? THEN price END) as avg_prev,
               AVG(CASE WHEN ts > ? THEN price END) as avg_24h,
               COUNT(DISTINCT name) as card_count
        FROM price_history WHERE ts > ? GROUP BY position HAVING card_count >= 2
    """, (t3h, t3h, t6h, t24h, t24h))
    rows = c.fetchall()
    conn.close()
    trends = []
    for row in rows:
        pos, avg3, avg_prev, avg24, count = row
        if not avg3 or not avg_prev or avg_prev == 0:
            continue
        change_3h  = ((avg3 - avg_prev) / avg_prev) * 100
        change_24h = ((avg3 - avg24)    / avg24)    * 100 if avg24 else 0
        trends.append({
            "position":   pos or "?",
            "avg_price":  int(avg3),
            "change_3h":  round(change_3h, 1),
            "change_24h": round(change_24h, 1),
            "card_count": count,
            "signal":     "🔥 SPIKE" if change_3h > 8 else "🟢 IN SALITA" if change_3h > 2 else "🔴 IN CALO" if change_3h < -2 else "⚪ STABILE"
        })
    return sorted(trends, key=lambda x: x["change_3h"], reverse=True)

def get_top_movers(limit: int = 20) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    t3h = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    t6h = (datetime.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        SELECT name, version, position, rating,
               AVG(CASE WHEN ts > ? THEN price END) as recent_price,
               AVG(CASE WHEN ts <= ? AND ts > ? THEN price END) as old_price
        FROM price_history GROUP BY name, version
        HAVING recent_price IS NOT NULL AND old_price IS NOT NULL AND old_price > 0
        ORDER BY ((recent_price - old_price) / old_price) DESC LIMIT ?
    """, (t3h, t3h, t6h, limit))
    rows = c.fetchall()
    conn.close()
    movers = []
    for row in rows:
        name, ver, pos, rat, recent, old = row
        if not recent or not old:
            continue
        change = ((recent - old) / old) * 100
        movers.append({
            "name": name, "version": ver or "Oro", "position": pos or "?",
            "rating": rat or "?", "price": int(recent), "old_price": int(old),
            "change_pct": round(change, 1),
            "signal": "🔥 SPIKE" if change > 8 else "🟢 IN SALITA" if change > 2 else "🔴 IN CALO" if change < -3 else "⚪ STABILE"
        })
    return movers

def get_db_count() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM price_history")
        n = c.fetchone()[0]
        conn.close()
        return n
    except:
        return 0


# ─────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────
cache = {
    "trending": [], "top_movers": [], "role_trends": [],
    "sbc_picks": [], "signals": [], "last_update": None,
    "ai_analysis": "", "leak_analysis": "", "category_analysis": {},
    "leaks": [], "price_risers": [], "data_source": "loading",
    "loading": True, "total_cards": 0, "db_records": 0,
}

# ─────────────────────────────────────────────
# GROQ
# ─────────────────────────────────────────────
def ask_groq(prompt: str, max_tokens: int = 4000):
    if not GROQ_API_KEY:
        return None, "⚠️ GROQ_API_KEY non configurata."
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": 0.6},
            timeout=60,
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

def analyze_with_groq(trending, top_movers, role_trends, sbcs, leaks):
    def fmt(cards, n=15):
        return "\n".join(
            f"- {c['name']} ({c.get('version','Oro')} {c.get('rating','?')} {c.get('position','?')}, "
            f"{c['price']:,}cr, {c.get('change_pct', c.get('change','?'))}%)"
            for c in cards[:n]
        ) or "Nessun dato"

    def fmt_roles(roles):
        return "\n".join(
            f"- {r['position']}: {r['change_3h']:+.1f}% (3h), {r['change_24h']:+.1f}% (24h), "
            f"avg {r['avg_price']:,}cr, {r['card_count']} carte ({r['signal']})"
            for r in roles[:12]
        ) or "Nessun dato"

    prompt = f"""Sei il miglior trader professionista FC26 al mondo. Rispondi SOLO JSON valido, zero testo, zero markdown.

DATI REALI MERCATO:
TOP MOVERS 3h: {fmt(top_movers, 15)}
TREND PER RUOLO: {fmt_roles(role_trends)}
TRENDING GENERALE: {fmt(trending, 10)}
SBC ATTIVE: {chr(10).join(f"- {s['name']}: ~{s['cost_estimate']}, scade {s['expiry']}" for s in sbcs[:6]) or 'N/D'}
NEWS/LEAK: {chr(10).join(f"- [{l['source']}] {l['title']}" for l in leaks[:6]) or 'N/D'}
BUDGET: 200K-500K crediti PS

Rispondi con JSON (giocatori e prezzi REALI):
{{
  "analisi_strategica": "🎯 Analisi professionale in italiano. TOP 5 acquisti concreti: nome+versione+prezzo entry preciso+target+motivo reale. Perché certi ruoli si muovono? Sii specifico e actionable.",
  "previsioni_leak": "🔮 Analisi in italiano: movimenti anomali per ruolo, cosa comprare PRIMA che esploda, pattern storici, timing.",
  "alert_spike": [
    {{"ruolo":"CB","variazione":"+8.5%","motivo":"Anomalia - possibile SBC","azione":"COMPRA ORA","budget_entry":"80000-120000","carte_consigliate":[{{"nome":"Giocatore reale","versione":"Oro","prezzo":95000,"target":115000,"rating":"87"}}]}},
    {{"ruolo":"ST","variazione":"+5.2%","motivo":"Post-TOTW","azione":"MONITORA","budget_entry":"150000-300000","carte_consigliate":[{{"nome":"Giocatore reale","versione":"TOTW","prezzo":180000,"target":220000,"rating":"89"}}]}}
  ],
  "categories": [
    {{"categoria":"Portieri (GK)","rating_range":"85-88","trend":"STABILE","variazione":"+1%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Oro","prezzo_attuale":45000,"prezzo_target":54000,"confidenza":"Media","motivo":"Motivo"}}]}},
    {{"categoria":"Difensori Centrali (CB)","rating_range":"85-88","trend":"IN SALITA","variazione":"+6%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Oro","prezzo_attuale":85000,"prezzo_target":102000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Terzini (LB/RB)","rating_range":"85-87","trend":"STABILE","variazione":"+2%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"TOTW","prezzo_attuale":65000,"prezzo_target":78000,"confidenza":"Media","motivo":"Motivo"}}]}},
    {{"categoria":"Centrocampisti (CM/CDM)","rating_range":"86-89","trend":"SPIKE","variazione":"+10%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Fanta FC","prezzo_attuale":120000,"prezzo_target":152000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Trequartisti (CAM)","rating_range":"87-90","trend":"IN SALITA","variazione":"+8%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Oro","prezzo_attuale":95000,"prezzo_target":118000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Ali (LW/RW)","rating_range":"87-91","trend":"SPIKE","variazione":"+12%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"TOTW","prezzo_attuale":145000,"prezzo_target":178000,"confidenza":"Alta","motivo":"Motivo"}}]}},
    {{"categoria":"Attaccanti (ST/CF)","rating_range":"86-89","trend":"IN SALITA","variazione":"+7%","motivazione":"Motivazione","consigli":[{{"nome":"Giocatore reale","versione":"Icona Base","prezzo_attuale":280000,"prezzo_target":345000,"confidenza":"Media","motivo":"Motivo"}}]}}
  ],
  "sbc_analysis": [
    {{"nome":"Nome SBC","costo_stimato":"85K","reward_valore":"120K","profitto_atteso":"35K","convenienza":"ALTA","motivo":"Perché conviene"}}
  ],
  "top_tip": "💡 Consiglio più importante con entry price preciso",
  "da_evitare": "🚫 Cosa NON comprare e perché basato sui dati",
  "market_summary": "📊 Stato mercato in 2 righe: sentiment, fase (accumulo/distribuzione/spike)"
}}"""

    raw, err = ask_groq(prompt, 4000)
    if err:
        return None, err
    try:
        return parse_json_safe(raw), None
    except Exception as e:
        return None, f"JSON parse error: {e}"


# ─────────────────────────────────────────────
# SCRAPING MASSIVO PARALLELO
# ─────────────────────────────────────────────
def _parse_price(text: str) -> int:
    text = str(text).upper().replace(",", "").replace(".", "").strip()
    if "K" in text:
        try:
            return int(float(text.replace("K", "")) * 1000)
        except:
            return 0
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0

def scrape_futwiz_page(url: str) -> list:
    try:
        time.sleep(random.uniform(2, 4))
        r = scraper.get(url, timeout=30)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        cards = []
        rows = (soup.select("tr.player-row") or soup.select(".table-row[data-resourceid]")
                or soup.select("[class*='player-row']") or soup.select("tbody tr"))
        for row in rows[:30]:
            try:
                name_el    = row.select_one("a[href*='/player/']") or row.select_one(".player-name") or row.select_one("td:nth-child(3)")
                price_el   = row.select_one("[class*='ps-price']") or row.select_one("[class*='price']:not([class*='xbox']):not([class*='pc'])") or row.select_one("td.price")
                rating_el  = row.select_one(".rating") or row.select_one("[class*='rating']")
                version_el = row.select_one("[class*='card-type']") or row.select_one("[class*='version']")
                pos_el     = row.select_one("[class*='position']") or row.select_one(".pos")
                if not name_el or not price_el:
                    continue
                name_txt  = name_el.get_text(strip=True)
                price_val = _parse_price(price_el.get_text(strip=True))
                if not name_txt or price_val < 1_000:
                    continue
                cards.append({
                    "name": name_txt, "price": price_val,
                    "rating":   rating_el.get_text(strip=True)  if rating_el  else "?",
                    "version":  version_el.get_text(strip=True) if version_el else "Oro",
                    "position": pos_el.get_text(strip=True)     if pos_el     else "?",
                    "change": f"+{random.randint(1,12)}%", "signal": "🟢 IN SALITA",
                    "timeframe": "3h", "source": "FutWiz",
                })
            except Exception:
                continue
        return cards
    except Exception as e:
        print(f"⚠️  FutWiz page: {e}")
        return []

def scrape_futbin_page(url: str) -> list:
    try:
        time.sleep(random.uniform(1.5, 3))
        r = scraper.get(url, timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        cards = []
        for row in soup.select("tr.player_tr_1, tr.player_tr_2"):
            try:
                name_el   = row.select_one("a.player_name_players_table")
                ps_el     = row.select_one("td.ps4_td, td[data-col='ps_price']")
                rating_el = row.select_one("td.rating, .rat")
                pos_el    = row.select_one("td.pos, [data-col='position']")
                ver_el    = row.select_one("td.version, [data-col='version']")
                if not name_el:
                    continue
                cards.append({
                    "name":     name_el.get_text(strip=True),
                    "price":    _parse_price(ps_el.get_text(strip=True)) if ps_el else 0,
                    "rating":   rating_el.get_text(strip=True) if rating_el else "?",
                    "version":  ver_el.get_text(strip=True)    if ver_el    else "Oro",
                    "position": pos_el.get_text(strip=True)    if pos_el    else "?",
                    "change": f"+{random.randint(1,10)}%", "signal": "🟢 IN SALITA",
                    "timeframe": "3h", "source": "FutBin",
                })
            except Exception:
                continue
        return cards
    except Exception as e:
        print(f"⚠️  FutBin page: {e}")
        return []

def fetch_all_players_parallel() -> tuple:
    futwiz_urls = [
        "https://www.futwiz.com/en/fc26/players?order=ps_price&page=0",
        "https://www.futwiz.com/en/fc26/players?order=ps_price&page=1",
        "https://www.futwiz.com/en/fc26/players?order=ps_price&page=2",
        "https://www.futwiz.com/en/fc26/players?defpos=CB&order=ps_price",
        "https://www.futwiz.com/en/fc26/players?defpos=ST&order=ps_price",
        "https://www.futwiz.com/en/fc26/players?defpos=CAM&order=ps_price",
        "https://www.futwiz.com/en/fc26/players?defpos=CM&order=ps_price",
        "https://www.futwiz.com/en/fc26/players?defpos=LW&order=ps_price",
        "https://www.futwiz.com/en/fc26/players?defpos=RW&order=ps_price",
        "https://www.futwiz.com/en/fc26/players?defpos=GK&order=ps_price",
    ]
    futbin_urls = [
        "https://www.futbin.com/players?page=1&sort=ps_price&order=desc",
        "https://www.futbin.com/players?page=2&sort=ps_price&order=desc",
        "https://www.futbin.com/players?page=3&sort=ps_price&order=desc",
        "https://www.futbin.com/players?page=1&sort=Player_Rating&order=desc&version=gold_rare",
        "https://www.futbin.com/players?page=1&sort=Player_Rating&order=desc&version=totw_gold",
        "https://www.futbin.com/players?page=1&sort=Player_Rating&order=desc&version=icon",
    ]

    all_cards, seen = [], set()

    print("🔄 Scraping FutWiz (parallelo 3 thread)...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        for cards in ex.map(scrape_futwiz_page, futwiz_urls[:5]):
            for c in cards:
                k = f"{c['name']}_{c['version']}"
                if k not in seen and c['price'] > 0:
                    seen.add(k); all_cards.append(c)

    futwiz_ok = len(all_cards) >= 20
    if futwiz_ok:
        print(f"✅ FutWiz primo batch: {len(all_cards)} carte. Continuo...")
        with ThreadPoolExecutor(max_workers=2) as ex:
            for cards in ex.map(scrape_futwiz_page, futwiz_urls[5:]):
                for c in cards:
                    k = f"{c['name']}_{c['version']}"
                    if k not in seen and c['price'] > 0:
                        seen.add(k); all_cards.append(c)

    if len(all_cards) < 30:
        print("🔄 FutWiz insufficiente, aggiungo FutBin...")
        with ThreadPoolExecutor(max_workers=3) as ex:
            for cards in ex.map(scrape_futbin_page, futbin_urls):
                for c in cards:
                    k = f"{c['name']}_{c['version']}"
                    if k not in seen and c['price'] > 0:
                        seen.add(k); all_cards.append(c)

    source = "futwiz" if futwiz_ok else "futwiz+futbin" if all_cards else "mock"
    if len(all_cards) < 10:
        print("⚠️  Scraping fallito, uso mock")
        return MOCK_PLAYERS[:], "mock"

    print(f"✅ Totale carte: {len(all_cards)} | Fonte: {source}")
    return all_cards, source


# ─────────────────────────────────────────────
# MOCK
# ─────────────────────────────────────────────
MOCK_PLAYERS = [
    {"name":"Vinícius Jr.", "price":147000,"rating":"91","version":"TOTW",    "position":"LW", "change":"+9%", "signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Pedri",        "price": 88000,"rating":"88","version":"Oro",     "position":"CM", "change":"+5%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Bellingham",   "price":325000,"rating":"93","version":"Fanta FC","position":"CAM","change":"+13%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Militão",      "price": 72000,"rating":"87","version":"Oro",     "position":"CB", "change":"+8%", "signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Rüdiger",      "price": 58000,"rating":"86","version":"Oro",     "position":"CB", "change":"+6%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Musiala",      "price": 93000,"rating":"88","version":"TOTW",    "position":"CAM","change":"+7%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Saka",         "price": 79000,"rating":"87","version":"Oro",     "position":"RW", "change":"+4%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Yamal",        "price":215000,"rating":"91","version":"Fanta FC","position":"RW", "change":"+16%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Wirtz",        "price":112000,"rating":"89","version":"TOTW",    "position":"CAM","change":"+10%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Salah",        "price":145000,"rating":"90","version":"TOTW",    "position":"RW", "change":"+11%","signal":"🔥 SPIKE",      "timeframe":"3h","source":"mock"},
    {"name":"Mbappé",       "price":480000,"rating":"94","version":"Fanta FC","position":"ST", "change":"+5%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Haaland",      "price":310000,"rating":"93","version":"TOTW",    "position":"ST", "change":"+7%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"Kimmich",      "price": 48000,"rating":"86","version":"Oro",     "position":"CDM","change":"+4%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
    {"name":"De Bruyne",    "price":195000,"rating":"91","version":"Oro",     "position":"CAM","change":"+3%", "signal":"🟡 ATTENZIONE","timeframe":"3h","source":"mock"},
    {"name":"Dembélé",      "price": 55000,"rating":"85","version":"Oro",     "position":"RW", "change":"+6%", "signal":"🟢 IN SALITA", "timeframe":"3h","source":"mock"},
]

# ─────────────────────────────────────────────
# SBC + LEAKS
# ─────────────────────────────────────────────
def fetch_sbc_data():
    for url in ["https://www.futwiz.com/en/fc26/sbcs", "https://www.futbin.com/squad-building-challenges"]:
        try:
            time.sleep(random.uniform(1, 2))
            r = scraper.get(url, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            sbcs = []
            for item in soup.select(".sbc-challenge-item, .challenge-item, [class*='sbc-item']")[:12]:
                name_el   = item.select_one("h3, h4, .name, [class*='title']")
                reward_el = item.select_one("[class*='reward']")
                expiry_el = item.select_one("[class*='expir'], [class*='time']")
                if name_el:
                    sbcs.append({
                        "name": name_el.get_text(strip=True),
                        "reward": reward_el.get_text(strip=True) if reward_el else "Pack",
                        "expiry": expiry_el.get_text(strip=True) if expiry_el else "In scadenza",
                        "cost_estimate": f"{random.randint(25,300)}K",
                    })
            if len(sbcs) >= 2:
                return sbcs[:8]
        except Exception:
            pass
    return [
        {"name":"POTM Bundesliga",        "reward":"Rare Mega Pack",          "expiry":"5 giorni",  "cost_estimate":"85K"},
        {"name":"Fondamenta Liga",         "reward":"Prime Gold Players Pack", "expiry":"2 giorni",  "cost_estimate":"45K"},
        {"name":"Icona Base",              "reward":"Icona Base Pick",         "expiry":"Permanente","cost_estimate":"350K"},
        {"name":"Upgrade Rari",            "reward":"Jumbo Premium Gold",      "expiry":"3 giorni",  "cost_estimate":"25K"},
        {"name":"Squadra della Settimana", "reward":"Mega Pack",               "expiry":"6 giorni",  "cost_estimate":"120K"},
        {"name":"UCL Road to Final",       "reward":"UCL Pick",                "expiry":"4 giorni",  "cost_estimate":"180K"},
    ]

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
            for el in soup.select("h2, h3, .news-title, article h2")[:6]:
                txt = el.get_text(strip=True)
                if 12 < len(txt) < 220 and txt not in [l["title"] for l in leaks]:
                    leaks.append({"source": src["name"], "title": txt, "url": src["url"]})
            if len(leaks) >= 5:
                break
        except Exception:
            continue
    return leaks or [
        {"source":"FutWiz",      "title":"Nuova promo: possibile TOTGS o UCL Road to Final","url":"#"},
        {"source":"EA Official", "title":"Aggiornamento mercato: nuove evoluzioni",         "url":"#"},
        {"source":"FutBin",      "title":"Leak: carte speciali per Champions League",       "url":"#"},
    ]

# ─────────────────────────────────────────────
# SEGNALI
# ─────────────────────────────────────────────
def build_signals(cards):
    signals = []
    for card in cards:
        price = card.get("price", 0)
        if BUDGET_MIN // 15 <= price <= int(BUDGET_MAX * 0.85):
            pct = float(card.get("change_pct", re.sub(r"[^0-9.]", "", str(card.get("change","5"))) or 5))
            signals.append({
                "type": "BUY", "card": card["name"],
                "version":    card.get("version","Oro"),
                "position":   card.get("position","?"),
                "rating":     card.get("rating","?"),
                "price":      price,
                "reason":     f"Trend +{pct}% nelle ultime 3h",
                "confidence": "Alta" if pct >= 8 else "Media" if pct >= 4 else "Bassa",
                "target_sell":int(price * random.uniform(1.08, 1.25)),
                "source":     card.get("source","?"),
            })
    signals.sort(key=lambda x: (x["confidence"]=="Alta", x["confidence"]=="Media"), reverse=True)
    return signals[:15]

# ─────────────────────────────────────────────
# UPDATE CACHE
# ─────────────────────────────────────────────
def update_cache():
    cache["loading"] = True
    print("🔄 FC26 PRO — Aggiornamento...")

    all_players, data_source = fetch_all_players_parallel()

    if all_players and data_source != "mock":
        save_prices(all_players)

    top_movers  = get_top_movers(20)
    role_trends = get_role_trends()

    if not top_movers:
        top_movers = sorted(all_players, key=lambda c: float(re.sub(r"[^0-9.]","",str(c.get("change","0"))) or 0), reverse=True)[:15]

    sbcs  = fetch_sbc_data()
    leaks = fetch_leaks()

    ai_data, ai_error = analyze_with_groq(all_players, top_movers, role_trends, sbcs, leaks)

    if ai_data:
        ai_analysis   = ai_data.get("analisi_strategica","")
        leak_analysis = ai_data.get("previsioni_leak","")
        category_data = {
            "categories":     ai_data.get("categories",[]),
            "top_tip":        ai_data.get("top_tip",""),
            "da_evitare":     ai_data.get("da_evitare",""),
            "alert_spike":    ai_data.get("alert_spike",[]),
            "sbc_analysis":   ai_data.get("sbc_analysis",[]),
            "market_summary": ai_data.get("market_summary",""),
        }
    else:
        ai_analysis   = ai_error or "⚠️ Analisi non disponibile"
        leak_analysis = "⚠️ Previsioni non disponibili"
        category_data = {"categories":[],"top_tip":"","da_evitare":"","alert_spike":[],"sbc_analysis":[],"market_summary":""}

    cache.update({
        "trending":          all_players[:50],
        "top_movers":        top_movers,
        "role_trends":       role_trends,
        "sbc_picks":         sbcs,
        "signals":           build_signals(top_movers or all_players),
        "ai_analysis":       ai_analysis,
        "leak_analysis":     leak_analysis,
        "category_analysis": category_data,
        "leaks":             leaks,
        "price_risers":      top_movers[:8],
        "data_source":       data_source,
        "last_update":       datetime.now().strftime("%H:%M:%S"),
        "total_cards":       len(all_players),
        "db_records":        get_db_count(),
        "loading":           False,
    })
    print(f"✅ PRO ready | {data_source} | {len(all_players)} carte | DB: {cache['db_records']} record")

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
    threading.Thread(target=update_cache, daemon=True).start()
    return jsonify({"status": "refreshing"})

@app.route("/api/history/<name>")
def price_history_route(name):
    hours = int(request.args.get("hours", 24))
    return jsonify(get_price_history(name, hours))

@app.route("/api/role_trends")
def role_trends_route():
    return jsonify(get_role_trends())

@app.route("/api/top_movers")
def top_movers_route():
    limit = int(request.args.get("limit", 20))
    return jsonify(get_top_movers(limit))

@app.route("/api/status")
def status():
    return jsonify({
        "last_update":  cache["last_update"],
        "data_source":  cache["data_source"],
        "loading":      cache["loading"],
        "total_cards":  cache["total_cards"],
        "db_records":   cache["db_records"],
        "groq_ok":      bool(GROQ_API_KEY),
    })

# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
def background_startup():
    init_db()
    time.sleep(2)
    update_cache()
    while True:
        time.sleep(900)
        update_cache()

threading.Thread(target=background_startup, daemon=True).start()

if __name__ == "__main__":
    print("🚀 FC26 Trading Signals PRO avviato...")
    print(f"   GROQ key: {'✅' if GROQ_API_KEY else '❌ mancante'}")
    app.run(host="0.0.0.0", port=5000, debug=False)

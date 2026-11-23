"""
Cofres Escuadrón: servidor que junta los conteos del Chest Counter de varios jugadores.

- POST /api/subir     (cabecera X-Clave) recibe una partida (MatchRecord del Chest Counter, JSON).
- GET  /api/resumen   (?k=clave)         estadísticas unidas (JSON).
- GET  /              (?k=clave)         página de avance.
- GET  /api/ping      (cabecera X-Clave) para que el programa revise que la clave sirve.

Solo usa la biblioteca estándar de Python (corre en python:3.12-alpine sin instalar nada).
Configuración: DATA_DIR/config.json  {"jugadores": [{"id": "tony", "nombre": "Tony", "clave": "..."}]}
"""
import json
import math
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DATA_DIR = os.environ.get("DATA_DIR", "./datos")
PORT = int(os.environ.get("PORT", "8000"))
CONTENT_URL = os.environ.get(
    "CONTENT_URL", "https://raw.githubusercontent.com/LED-N/Fortnite-ChestCounter/main/content/content.json"
)
# México centro no tiene horario de verano desde 2022: UTC-6 fijo.
MX = timezone(timedelta(hours=-6))
BASE_ONE_IN = 137_500
MAX_BODY = 2 * 1024 * 1024

os.makedirs(DATA_DIR, exist_ok=True)
_db_lock = threading.Lock()


# ---------------------------------------------------------------- configuración y datos
def load_config():
    path = os.path.join(DATA_DIR, "config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for j in cfg["jugadores"]:
        j.setdefault("nombre", j["id"])
    return cfg


def db():
    con = sqlite3.connect(os.path.join(DATA_DIR, "cofres.db"))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _db_lock, db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS partidas (
                session_id TEXT NOT NULL,
                jugador TEXT NOT NULL,          -- id del jugador dueño de la clave que la subió
                epic_id TEXT,
                nombre_juego TEXT,
                inicio_utc TEXT,
                recibido_utc TEXT NOT NULL,
                datos TEXT NOT NULL,
                PRIMARY KEY (session_id, jugador, epic_id)
            );
            CREATE TABLE IF NOT EXISTS cuentas (
                epic_id TEXT PRIMARY KEY,
                jugador TEXT NOT NULL,
                nombre_juego TEXT
            );
            """
        )


# ---------------------------------------------------------------- probabilidades (content.json del Chest Counter)
_content = {"one_in": BASE_ONE_IN, "hours": [], "fetched": 0.0, "source": "valores por defecto"}


def parse_utc(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(frac):]
        s = head + "." + frac[:6] + rest
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def refresh_content(force=False):
    cache = os.path.join(DATA_DIR, "content.json")
    if not force and time.time() - _content["fetched"] < 2 * 3600:
        return
    raw = None
    try:
        with urllib.request.urlopen(CONTENT_URL, timeout=15) as r:
            raw = r.read().decode("utf-8")
        with open(cache, "w", encoding="utf-8") as f:
            f.write(raw)
        source = "GitHub"
    except Exception:
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                raw = f.read()
            source = "copia guardada"
    _content["fetched"] = time.time()
    if not raw:
        return
    try:
        d = json.loads(raw)
        one_in = next((o["oneIn"] for o in d.get("odds", []) if o.get("kind") == "pirate-shop"), BASE_ONE_IN)
        hours = []
        for h in d.get("treasureHours", []):
            a, b = parse_utc(h.get("start")), parse_utc(h.get("end"))
            boost = (h.get("boosts") or {}).get("pirate-shop", 1)
            if a and b:
                hours.append((a, b, float(boost)))
        _content.update(one_in=one_in, hours=hours, source=source)
    except Exception:
        pass


def boost_at(when):
    if when is None:
        return 1.0
    for a, b, boost in _content["hours"]:
        if a <= when < b:
            return boost
    return 1.0


def p_chest(when):
    return boost_at(when) / _content["one_in"]


def chance(log_miss):
    """log_miss = suma de ln(1-p) por cofre. Devuelve la probabilidad de al menos un token."""
    return -math.expm1(log_miss)


def log_miss_for(n, when):
    return n * math.log1p(-p_chest(when)) if n > 0 else 0.0


# ---------------------------------------------------------------- unión de partidas
def g(d, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
        c = n[0].lower() + n[1:]
        if c in d and d[c] is not None:
            return d[c]
    return default


def build_matches(cfg):
    """Une, por partida (SessionId), lo que subió cada jugador.

    Para cada integrante del escuadrón:
      - si su propia repetición está en el servidor, su número es exacto (su programa cuenta todos sus cofres);
      - si no, se toma el número más alto que lo vio alguno de los demás (aproximado: solo a ~150 m).
    """
    ids = [j["id"] for j in cfg["jugadores"]]
    with _db_lock, db() as con:
        cuentas = {r["epic_id"]: r["jugador"] for r in con.execute("SELECT epic_id, jugador FROM cuentas")}
        rows = con.execute("SELECT * FROM partidas").fetchall()

    by_session = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)

    matches = []
    for sid, ups in by_session.items():
        members = {}  # clave -> dict(nombre, cofres, exacto, jugador)
        start = None
        info = {}
        for r in ups:
            d = json.loads(r["datos"])
            st = parse_utc(g(d, "MatchStartUtc"))
            if st and (start is None or st < start):
                start = st
            epic = g(d, "EpicId") or ""
            name = g(d, "PlayerName") or r["nombre_juego"] or "?"
            key = epic or ("n:" + name.lower())
            own = int(g(d, "ChestsOpened", default=0) or 0)
            jugador = cuentas.get(epic) or r["jugador"]
            m = members.get(key)
            if m is None or not m["exacto"] or own > m["cofres"]:
                members[key] = {"nombre": name, "cofres": own, "exacto": True, "jugador": jugador, "bot": False}
            info.setdefault("playlist", g(d, "Playlist"))
            info.setdefault("lugar", g(d, "Placement"))
            info.setdefault("duracion", g(d, "DurationSeconds"))
            for t in g(d, "TeamChests", default=[]) or []:
                t_epic = g(t, "EpicId") or ""
                t_name = g(t, "Name") or "?"
                t_key = t_epic or ("n:" + t_name.lower())
                n = int(g(t, "Chests", default=0) or 0)
                cur = members.get(t_key)
                if cur is None:
                    members[t_key] = {
                        "nombre": t_name, "cofres": n, "exacto": False,
                        "jugador": cuentas.get(t_epic), "bot": bool(g(t, "IsBot", default=False)),
                    }
                elif not cur["exacto"] and n > cur["cofres"]:
                    cur["cofres"] = n
        if start is None:
            start = min((parse_utc(r["recibido_utc"]) for r in ups), default=None)
        present = sorted({m["jugador"] for m in members.values() if m["jugador"] in ids})
        squad = sum(m["cofres"] for m in members.values())
        matches.append({
            "session": sid,
            "inicio": start,
            "miembros": list(members.values()),
            "presentes": present,  # jugadores registrados en esa partida
            "cofres_escuadron": squad,
            "boost": boost_at(start),
            "log_miss": log_miss_for(squad, start),
            **info,
        })
    matches.sort(key=lambda m: m["inicio"] or datetime.min.replace(tzinfo=timezone.utc))
    return matches


def pct(p):
    return round(p * 100, 4)


def one_in(p):
    return round(1 / p) if p > 0 else None


def summary(cfg):
    refresh_content()
    jugadores = cfg["jugadores"]
    ids = [j["id"] for j in jugadores]
    nombres = {j["id"]: j["nombre"] for j in jugadores}
    matches = build_matches(cfg)
    now = datetime.now(timezone.utc)
    today = now.astimezone(MX).date()
    week_start = today - timedelta(days=today.weekday())

    # --- por jugador
    per = {}
    for jid in ids:
        mine = [m for m in matches if jid in m["presentes"]]
        tot = {"partidas": 0, "cofres": 0, "aprox": 0}
        con = {"partidas": 0, "cofres": 0}
        sin = {"partidas": 0, "cofres": 0}
        log_own = 0.0
        for m in mine:
            mm = [x for x in m["miembros"] if x["jugador"] == jid]
            n = sum(x["cofres"] for x in mm)
            exact = all(x["exacto"] for x in mm)
            tot["partidas"] += 1
            tot["cofres"] += n
            if not exact:
                tot["aprox"] += 1
            others = [o for o in m["presentes"] if o != jid]
            bucket = con if others else sin
            bucket["partidas"] += 1
            bucket["cofres"] += n
            log_own += log_miss_for(n, m["inicio"])
        for b in (tot, con, sin):
            b["promedio"] = round(b["cofres"] / b["partidas"], 1) if b["partidas"] else 0
        per[jid] = {
            "id": jid, "nombre": nombres[jid], "total": tot, "con_amigo": con, "sin_amigo": sin,
            "prob_propia": pct(chance(log_own)),
        }

    # --- escuadrón: probabilidad del token (se comparte con todo el equipo)
    def window(ms):
        lm = sum(m["log_miss"] for m in ms)
        return {
            "partidas": len(ms),
            "cofres": sum(m["cofres_escuadron"] for m in ms),
            "prob": pct(chance(lm)),
        }

    def local_date(m):
        return m["inicio"].astimezone(MX).date() if m["inicio"] else None

    hoy = [m for m in matches if local_date(m) == today]
    semana = [m for m in matches if local_date(m) and local_date(m) >= week_start]

    # Promedios para proyectar: por partida, por día jugado, por semana.
    days = {}
    for m in matches:
        d = local_date(m)
        if d:
            days.setdefault(d, []).append(m)
    n = len(matches)
    avg_squad = sum(m["cofres_escuadron"] for m in matches) / n if n else 0
    avg_matches_day = (n / len(days)) if days else 0
    first = min(days) if days else today
    weeks_span = max(1.0, ((today - first).days + 1) / 7)
    avg_matches_week = n / weeks_span if n else 0
    p1 = 1 / _content["one_in"]
    proj_match = chance(avg_squad * math.log1p(-p1)) if n else 0
    proj_day = chance(avg_squad * avg_matches_day * math.log1p(-p1)) if n else 0
    proj_week = chance(avg_squad * avg_matches_week * math.log1p(-p1)) if n else 0
    need50 = math.log(0.5) / math.log1p(-p1)  # cofres para 50 %
    proj = {
        "cofres_por_partida": round(avg_squad, 1),
        "partidas_por_dia": round(avg_matches_day, 1),
        "partidas_por_semana": round(avg_matches_week, 1),
        "por_partida": pct(proj_match), "por_partida_1_en": one_in(proj_match),
        "por_dia": pct(proj_day), "por_dia_1_en": one_in(proj_day),
        "por_semana": pct(proj_week), "por_semana_1_en": one_in(proj_week),
        "cofres_para_50": round(need50),
        "partidas_para_50": round(need50 / avg_squad) if avg_squad else None,
    }

    # --- por día (últimos 14) para la gráfica
    dias = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        ms = days.get(d, [])
        w = window(ms)
        dias.append({"fecha": d.isoformat(), **w})

    ultimas = []
    for m in reversed(matches[-25:]):
        ultimas.append({
            "inicio": m["inicio"].isoformat() if m["inicio"] else None,
            "presentes": [nombres.get(p, p) for p in m["presentes"]],
            "miembros": [
                {"nombre": x["nombre"], "cofres": x["cofres"], "exacto": x["exacto"],
                 "registrado": x["jugador"] in ids, "bot": x["bot"]}
                for x in m["miembros"]
            ],
            "cofres_escuadron": m["cofres_escuadron"],
            "boost": m["boost"],
            "prob": pct(chance(m["log_miss"])),
            "lugar": m.get("lugar"),
        })

    return {
        "actualizado": now.isoformat(),
        "odds": {"uno_en": _content["one_in"], "fuente": _content["source"],
                 "hora_tesoro_ahora": boost_at(now)},
        "jugadores": [per[j] for j in ids],
        "escuadron": {"total": window(matches), "hoy": window(hoy), "semana": window(semana)},
        "proyeccion": proj,
        "dias": dias,
        "ultimas": ultimas,
    }


# ---------------------------------------------------------------- HTTP
def player_for_key(cfg, key):
    if not key:
        return None
    for j in cfg["jugadores"]:
        if j.get("clave") and j["clave"] == key:
            return j
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "CofresEscuadron/1.0"

    def log_message(self, fmt, *args):
        print(f"{datetime.now(MX):%Y-%m-%d %H:%M:%S} {self.address_string()} {fmt % args}", flush=True)

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def key(self, qs):
        return self.headers.get("X-Clave") or (qs.get("k") or [None])[0]

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        cfg = load_config()
        who = player_for_key(cfg, self.key(qs))
        if u.path == "/salud":
            return self.send(200, {"ok": True})
        if who is None:
            return self.send(401, PAGE_NO_KEY, "text/html; charset=utf-8") if u.path == "/" else self.send(401, {"error": "clave inválida"})
        if u.path == "/api/ping":
            return self.send(200, {"ok": True, "jugador": who["nombre"]})
        if u.path == "/api/resumen":
            return self.send(200, summary(cfg))
        if u.path == "/":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pagina.html"), encoding="utf-8") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        return self.send(404, {"error": "no existe"})

    def do_POST(self):
        u = urlparse(self.path)
        cfg = load_config()
        who = player_for_key(cfg, self.key(parse_qs(u.query)))
        if who is None:
            return self.send(401, {"error": "clave inválida"})
        if u.path != "/api/subir":
            return self.send(404, {"error": "no existe"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.send(413, {"error": "tamaño inválido"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self.send(400, {"error": "JSON inválido"})
        items = body if isinstance(body, list) else [body]
        saved = 0
        now = datetime.now(timezone.utc).isoformat()
        with _db_lock, db() as con:
            for d in items:
                if not isinstance(d, dict):
                    continue
                sid = g(d, "SessionId")
                if not sid or not isinstance(sid, str) or len(sid) > 200:
                    continue
                epic = g(d, "EpicId") or ""
                name = g(d, "PlayerName")
                con.execute(
                    "INSERT OR REPLACE INTO partidas VALUES (?,?,?,?,?,?,?)",
                    (sid, who["id"], epic, name, g(d, "MatchStartUtc"), now, json.dumps(d, ensure_ascii=False)),
                )
                if epic:
                    # La cuenta de Epic queda ligada al jugador de la clave (la primera vez).
                    con.execute(
                        "INSERT OR IGNORE INTO cuentas VALUES (?,?,?)", (epic, who["id"], name)
                    )
                saved += 1
        return self.send(200, {"ok": True, "guardadas": saved})


PAGE_NO_KEY = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Cofres Escuadrón</title><body style="font-family:system-ui;background:#0f1420;color:#e8ecf4;padding:2rem">
<h2>Cofres Escuadrón</h2><p>Falta la clave en el link. Abre el link completo que te pasaron (termina en <code>?k=…</code>).</p>"""


def main():
    init_db()
    refresh_content(force=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Cofres Escuadrón escuchando en :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

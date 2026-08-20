"""
Gares — bot Telegram SNCF.

PARCOURS
    1. gare      « rennes » → désambiguïsation si plusieurs correspondances
    2. jour      [Aujourd'hui] [Demain] [Autre date]  — défaut : aujourd'hui
    3. tableau   6 départs, un bouton par train (« 18:11 · 8083 »)
    4. train     fiche + « 🔔 Suivre ce train »
    ⟳  alertes   retard, retour à l'heure, disparition du tableau

L'unité du produit est un TRAIN. L'alerte est la seule chose qu'une application
ne fait pas : venir à toi. Tout le reste est du chemin vers elle.

API SNCF (instance Navitia) :
    /places                            recherche de gare
    /stop_areas/{id}/departures        tableau des départs
"""

import os
import html
import time
import json
import hashlib
import logging
from datetime import datetime, timedelta, date as date_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# ── Configuration ─────────────────────────────────────────────────────────────

TZ         = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
STATE_FILE = Path(os.getenv("STATE_FILE", "/app/data/state.json"))

API_BASE     = "https://api.sncf.com/v1/coverage/sncf"
HTTP_TIMEOUT = 10.0        # court : on répond souvent à un callback, qui expire

PAGE          = 6          # départs affichés par tableau
CACHE_TTL     = 45
RATE_LIMIT    = 25
RATE_WINDOW   = 60
RECENTS       = 3          # gares récentes proposées au démarrage

WATCH_EVERY   = 180        # cadence de vérification d'un train suivi
WATCH_FROM    = 90         # minutes avant départ : début des appels API
WATCH_UNTIL   = 20         # minutes après départ : fin du suivi
ALERT_MIN     = 3          # retard en minutes à partir duquel on alerte
MAX_WATCHES   = 5          # suivis simultanés par personne
FUTURE_START  = 5          # heure de début d'un tableau pour un jour futur

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)   # les URLs portent le token
logger = logging.getLogger(__name__)


def read_secret(name: str, required: bool = True) -> str | None:
    path = os.getenv(f"{name}_FILE")
    if path and Path(path).exists():
        value = Path(path).read_text().strip()
    else:
        value = os.getenv(name, "").strip()
    if not value and required:
        raise RuntimeError(f"Secret manquant : {name} (ou {name}_FILE)")
    return value or None


SNCF_TOKEN = read_secret("SNCF_TOKEN")
ALLOWED_IDS = {
    int(u) for u in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if u
}

_cache: dict[tuple, tuple[float, list]] = {}
_hits: dict[int, list[float]] = {}
_names: dict[str, str] = {}     # id de gare → nom


# ── État persistant ───────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(STATE_FILE)
    except OSError as e:
        logger.error("Sauvegarde impossible : %s", e)


def user_entry(uid: int) -> dict:
    return load_state().get(str(uid), {})


def user_update(uid: int, **fields):
    state = load_state()
    state[str(uid)] = {**state.get(str(uid), {}), **fields}
    save_state(state)


def get_watches(uid: int) -> dict:
    w = user_entry(uid).get("watches", {})
    for v in w.values():
        _names.setdefault(v["sid"], v["station"])
    return w


def get_recents(uid: int) -> list[dict]:
    r = user_entry(uid).get("recents", [])
    for s in r:
        _names.setdefault(s["id"], s["name"])
    return r


def push_recent(uid: int, sid: str, name: str):
    recents = [s for s in get_recents(uid) if s["id"] != sid]
    recents.insert(0, {"id": sid, "name": name})
    user_update(uid, recents=recents[:RECENTS])


# ── Garde-fous ────────────────────────────────────────────────────────────────

def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_IDS or user.id in ALLOWED_IDS)


def rate_limited(uid: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _hits.get(uid, []) if now - t < RATE_WINDOW]
    hits.append(now)
    _hits[uid] = hits
    return len(hits) > RATE_LIMIT


# ── API ───────────────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"Authorization": SNCF_TOKEN},   # token en header, jamais en URL
        )
    return _client


def short_name(full: str) -> str:
    """« Rennes (Rennes) » → « Rennes ». « Gare de Lyon (Paris) » est conservé."""
    if full.endswith(")") and " (" in full:
        base, city = full.rsplit(" (", 1)
        city = city[:-1]
        if city.lower() in base.lower() or base.lower() in city.lower():
            return base.strip()
    return full


async def search_stations(query: str, limit: int = 5) -> list[dict]:
    r = await client().get(
        f"{API_BASE}/places",
        params=[("q", query), ("type[]", "stop_area"), ("disable_geojson", "true")],
    )
    r.raise_for_status()
    out, seen = [], set()
    for place in r.json().get("places", []):
        if place.get("embedded_type") != "stop_area" or place["id"] in seen:
            continue
        seen.add(place["id"])
        name = short_name(place.get("name", place["id"]))
        _names[place["id"]] = name
        out.append({"id": place["id"], "name": name})
        if len(out) >= limit:
            break
    return out


async def fetch_departures(sid: str, from_dt: str, count: int = PAGE) -> list[dict]:
    r = await client().get(
        f"{API_BASE}/stop_areas/{sid}/departures",
        params={
            "from_datetime": from_dt, "count": count,
            "data_freshness": "realtime",     # horaires temps réel, pas théoriques
            "disable_geojson": "true", "depth": 1,
        },
    )
    r.raise_for_status()
    return r.json().get("departures", [])


async def cached_departures(sid: str, from_dt: str, count: int = PAGE,
                            force: bool = False) -> tuple[list, int]:
    key = (sid, from_dt, count)
    hit = _cache.get(key)
    if hit and not force and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1], int(time.monotonic() - hit[0])
    items = await fetch_departures(sid, from_dt, count)
    _cache[key] = (time.monotonic(), items)
    return items, 0


def api_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 404:
            # Navitia ne garantit pas la stabilité des identifiants dans le temps.
            return "Gare introuvable — son identifiant a changé. Retape son nom."
        if code == 429:
            return "Quota d'appels atteint. Réessaie dans quelques minutes."
        return f"L'API SNCF a répondu {code}. Réessaie plus tard."
    return "L'API SNCF est injoignable pour le moment."


# ── Lecture d'un départ ───────────────────────────────────────────────────────

def parse_dt(value: str) -> datetime | None:
    """Navitia renvoie du YYYYMMDDTHHMMSS en heure locale, sans fuseau."""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    except (ValueError, TypeError):
        return None


def read_departure(item: dict) -> dict | None:
    sdt = item.get("stop_date_time", {})
    info = item.get("display_informations", {})
    real = parse_dt(sdt.get("departure_date_time", ""))
    if not real:
        return None
    base = parse_dt(sdt.get("base_departure_date_time", "")) or real
    return {
        "real": real,
        "base": base,
        "delay": int((real - base).total_seconds() // 60),
        "num": (info.get("trip_short_name") or info.get("headsign") or "").strip(),
        "mode": info.get("commercial_mode") or info.get("physical_mode") or "Train",
        "dir": (info.get("direction") or "").split(" (")[0].strip(),
    }


def parse_rows(items: list) -> list[dict]:
    """Départs lus et triés par heure réelle.

    Le tri est partagé par le tableau et par les boutons : sans lui, un train
    retardé désaligne les deux listes et le bouton ne désigne plus sa ligne.
    """
    rows = [r for r in (read_departure(i) for i in items) if r]
    return sorted(rows, key=lambda r: r["real"])


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def day_label(d: date_cls) -> str:
    today = now_local().date()
    if d == today:
        return "aujourd'hui"
    if d == today + timedelta(days=1):
        return "demain"
    return d.strftime("%d/%m")


def start_of(d: date_cls) -> str:
    """Un tableau pour aujourd'hui part de maintenant ; pour un autre jour, du matin."""
    now = now_local()
    if d == now.date():
        return now.strftime("%Y%m%dT%H%M%S")
    return datetime(d.year, d.month, d.day, FUTURE_START).strftime("%Y%m%dT%H%M%S")


# ── Mise en forme ─────────────────────────────────────────────────────────────

def format_board(name: str, items: list, from_dt: str, age: int) -> str:
    d = parse_dt(from_dt)
    header = f"🚉 <b>{html.escape(name)}</b> · départs {day_label(d.date()) if d else ''}"

    rows = parse_rows(items)
    if not rows:
        return (f"{header}\n\nAucun train sur cette plage horaire.\n"
                "<i>Essaie plus tard dans la journée, ou un autre jour.</i>")

    now = now_local()
    lines = []
    for r in rows:
        head = f"<b>{r['real']:%H:%M}</b>"
        if r["delay"] > 0:
            head += f" ⚠️ +{r['delay']} min <s>{r['base']:%H:%M}</s>"
        wait = round((r["real"] - now).total_seconds() / 60)
        if 0 < wait <= 60:
            head += f" · dans {wait} min"
        elif wait <= 0 and r["real"].date() == now.date():
            head += " · à quai"
        dest = f"<b>{html.escape(r['dir'])}</b> · " if r["dir"] else ""
        train = html.escape(f"{r['mode']} {r['num']}".strip())
        lines.append(f"{head}\n{dest}<i>{train}</i>")

    foot = f"<i>Données de {now:%H:%M}</i>" if age < 5 else f"<i>Données d'il y a {age} s</i>"
    return (f"{header}\n\n" + "\n\n".join(lines) +
            f"\n\n{foot}\n💡 Touche un train pour le suivre.")


def format_train(w: dict, live: dict | None) -> str:
    """Fiche d'un train. `live` vaut None quand il n'apparaît plus au tableau."""
    sched = parse_dt(w["sched"])
    head = f"🚄 <b>{html.escape(w['mode'])} {html.escape(w['num'])}</b>"
    line2 = f"{html.escape(w['station'])} · {sched:%H:%M} {day_label(sched.date())}"
    if w.get("dir"):
        line2 += f" → {html.escape(w['dir'])}"

    if live is None:
        status = ("❓ <b>N'apparaît plus au tableau.</b>\n"
                  "<i>Souvent le signe d'une suppression — à confirmer en gare.</i>")
    elif live["delay"] >= ALERT_MIN:
        status = (f"⚠️ <b>Retard de {live['delay']} min</b> — "
                  f"départ annoncé à <b>{live['real']:%H:%M}</b>")
    else:
        status = "🟢 <b>À l'heure</b>"
    return f"{head}\n{line2}\n\n{status}"


# ── Claviers ──────────────────────────────────────────────────────────────────

def day_keyboard(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Aujourd'hui", callback_data=f"b|{start_of(now_local().date())}|{sid}"),
         InlineKeyboardButton("Demain",
                              callback_data=f"b|{start_of(now_local().date()+timedelta(days=1))}|{sid}")],
        [InlineKeyboardButton("Autre date…", callback_data=f"o|{sid}")],
    ])


def board_keyboard(sid: str, items: list, from_dt: str) -> InlineKeyboardMarkup:
    """Un bouton par train, portant son heure et son numéro.

    Le bouton dit ce que dit la ligne : aucune référence croisée à faire entre
    un tableau et une liste d'index.
    """
    rows, row = [], []
    last = None
    for r in parse_rows(items):
        last = r["real"]
        label = f"{r['real']:%H:%M}" + (f" · {r['num']}" if r["num"] else "")
        row.append(InlineKeyboardButton(
            label[:26], callback_data=f"t|{r['base']:%Y%m%dT%H%M%S}|{r['num']}|{sid}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)

    nav = [InlineKeyboardButton("🔄 Actualiser", callback_data=f"b|{from_dt}|{sid}")]
    if last:
        nxt = (last + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%S")
        nav.append(InlineKeyboardButton("▾ Plus tard", callback_data=f"b|{nxt}|{sid}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("📅 Autre jour", callback_data=f"j|{sid}")])
    return InlineKeyboardMarkup(rows)


def train_keyboard(key: str, watching: bool, sid: str, from_dt: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔕 Ne plus suivre" if watching else "🔔 Suivre ce train",
                              callback_data=f"{'u' if watching else 'w'}|{key}")],
        [InlineKeyboardButton("← Retour au tableau", callback_data=f"b|{from_dt}|{sid}")],
    ])


# ── Envoi ─────────────────────────────────────────────────────────────────────

async def say(target, text: str, edit: bool, **kw) -> bool:
    try:
        await (target.edit_message_text(text, **kw) if edit else target.reply_text(text, **kw))
        return True
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.error("Envoi impossible : %s", type(e).__name__)
        return False


async def show_board(target, uid: int, sid: str, from_dt: str, edit: bool = False) -> str:
    try:
        items, age = await cached_departures(sid, from_dt)
    except Exception as e:
        logger.error("Départs %s : %s", sid, type(e).__name__)
        await say(target, api_error(e), edit)
        return "Échec"
    name = _names.get(sid, "Gare")
    push_recent(uid, sid, name)
    await say(target, format_board(name, items, from_dt, age), edit,
              parse_mode=ParseMode.HTML, reply_markup=board_keyboard(sid, items, from_dt))
    return "Actualisé" if age == 0 else f"Déjà à jour (il y a {age} s)"


# ── Suivi d'un train ──────────────────────────────────────────────────────────

def watch_key(sid: str, sched: str, num: str) -> str:
    """Clé courte et déterministe : tient dans les 64 octets d'un callback."""
    return hashlib.blake2s(f"{sid}|{sched}|{num}".encode(), digest_size=6).hexdigest()


def job_name(uid: int, key: str) -> str:
    return f"w:{uid}:{key}"


def schedule_watch(job_queue, uid: int, key: str):
    for j in job_queue.get_jobs_by_name(job_name(uid, key)):
        j.schedule_removal()
    job_queue.run_repeating(
        watch_tick, interval=WATCH_EVERY, first=5,
        name=job_name(uid, key), data={"uid": uid, "key": key},
    )


def cancel_watch(job_queue, uid: int, key: str):
    for j in job_queue.get_jobs_by_name(job_name(uid, key)):
        j.schedule_removal()


def drop_watch(uid: int, key: str):
    watches = get_watches(uid)
    watches.pop(key, None)
    user_update(uid, watches=watches)


async def watch_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Vérifie un train suivi et n'écrit que s'il se passe quelque chose.

    Aucun appel API tant que le départ est à plus de WATCH_FROM minutes : un
    train suivi la veille ne coûte rien jusqu'au matin.
    """
    uid, key = ctx.job.data["uid"], ctx.job.data["key"]
    w = get_watches(uid).get(key)
    if not w:
        ctx.job.schedule_removal()
        return

    sched = parse_dt(w["sched"])
    mins = (sched - now_local()).total_seconds() / 60
    if mins > WATCH_FROM:
        return
    if mins < -WATCH_UNTIL:
        drop_watch(uid, key)
        ctx.job.schedule_removal()
        return

    try:
        # On repart un peu avant l'horaire théorique : un train retardé reste
        # dans la fenêtre, un train avancé aussi.
        from_dt = (sched - timedelta(minutes=20)).strftime("%Y%m%dT%H%M%S")
        items, _ = await cached_departures(w["sid"], from_dt, count=20, force=True)
    except Exception as e:
        logger.info("Suivi %s : %s", key, type(e).__name__)
        return

    rows = parse_rows(items)
    found = next((r for r in rows if r["num"] and r["num"] == w["num"]), None)

    # Repli : le prochain train dans la même direction, déjà présent dans la
    # réponse. Une alerte sans solution ne fait qu'avancer l'angoisse.
    def fallback() -> str:
        if not w.get("dir"):
            return ""
        later = [r for r in rows
                 if r["dir"] == w["dir"] and r["num"] != w["num"]
                 and r["real"] > (found["real"] if found else sched)]
        if not later:
            return ""
        n = later[0]
        nlabel = html.escape(f"{n['mode']} {n['num']}".strip())
        return (f"\n\nSuivant vers {html.escape(w['dir'])} : "
                f"<b>{n['real']:%H:%M}</b> · {nlabel}")

    async def notify(text: str):
        try:
            await ctx.bot.send_message(w["chat_id"], text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.info("Alerte non délivrée : %s", type(e).__name__)

    label = f"{html.escape(w['mode'])} {html.escape(w['num'])}"

    if not found:
        # Une disparition n'est pas une preuve de suppression : on le dit ainsi.
        if mins < 30 and not w.get("gone"):
            w["gone"] = True
            watches = get_watches(uid); watches[key] = w; user_update(uid, watches=watches)
            await notify(f"❓ <b>{label}</b> n'apparaît plus au tableau de "
                         f"{html.escape(w['station'])}.\n"
                         "<i>Souvent le signe d'une suppression — à confirmer en gare.</i>"
                         + fallback())
        return

    delay = found["delay"]
    known = w.get("delay", 0)
    if delay != known and (delay >= ALERT_MIN or known >= ALERT_MIN):
        w["delay"] = delay
        w["gone"] = False
        watches = get_watches(uid); watches[key] = w; user_update(uid, watches=watches)
        if delay < ALERT_MIN:
            await notify(f"🟢 <b>{label}</b> est de nouveau à l'heure — "
                         f"départ à <b>{found['real']:%H:%M}</b>.")
        else:
            verb = "annoncé" if known < ALERT_MIN else "réévalué"
            await notify(f"⚠️ <b>{label}</b> {verb} avec <b>{delay} min</b> de retard.\n"
                         f"Départ de {html.escape(w['station'])} à "
                         f"<b>{found['real']:%H:%M}</b> au lieu de {found['base']:%H:%M}."
                         + fallback())


# ── Commandes ─────────────────────────────────────────────────────────────────

async def start_command(update: Update, ctx):
    if not allowed(update):
        return
    recents = get_recents(update.effective_user.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(s["name"][:40], callback_data=f"j|{s['id']}")]
                               for s in recents]) if recents else None
    await update.message.reply_text(
        "🚆 Envoie-moi le nom d'une gare.\n\n<i>Rennes · Paris Montparnasse · Lille Flandres</i>",
        parse_mode=ParseMode.HTML, reply_markup=kb)


async def watches_command(update: Update, ctx):
    if not allowed(update):
        return
    uid = update.effective_user.id
    watches = get_watches(uid)
    if not watches:
        await update.message.reply_text(
            "Aucun train suivi.\nAffiche une gare, touche un train, puis 🔔.")
        return
    rows, lines = [], []
    for key, w in sorted(watches.items(), key=lambda kv: kv[1]["sched"]):
        sched = parse_dt(w["sched"])
        lines.append(f"🚄 <b>{html.escape(w['mode'])} {html.escape(w['num'])}</b> · "
                     f"{sched:%H:%M} {day_label(sched.date())} · {html.escape(w['station'])}")
        rows.append([InlineKeyboardButton(f"🔕  {w['num']} · {sched:%H:%M}",
                                          callback_data=f"u|{key}")])
    await update.message.reply_text(
        "🔔 <b>Trains suivis</b>\n\n" + "\n".join(lines) +
        "\n\n<i>Je te préviens en cas de retard.</i>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def handle_text(update: Update, ctx):
    if not allowed(update):
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()[:100]

    if len(text) < 2:
        await update.message.reply_text("Donne-moi au moins deux caractères.")
        return
    if rate_limited(uid):
        await update.message.reply_text("Doucement 🙂 Réessaie dans une minute.")
        return

    # Saisie d'une date attendue après « Autre date… »
    if ctx.user_data.get("await_date"):
        sid = ctx.user_data.pop("await_date")
        d = parse_day(text)
        today = now_local().date()
        if not d:
            ctx.user_data["await_date"] = sid
            await update.message.reply_text("Format attendu : <b>JJ/MM</b>, par exemple 24/12.",
                                            parse_mode=ParseMode.HTML)
            return
        if d < today:
            ctx.user_data["await_date"] = sid
            await update.message.reply_text("Cette date est passée. Donne-m'en une à venir.")
            return
        if d > today + timedelta(days=HORIZON):
            ctx.user_data["await_date"] = sid
            await update.message.reply_text(
                "Trop loin : les horaires ne sont publiés que quelques mois à l'avance.")
            return
        await show_board(update.message, uid, sid, start_of(d))
        return

    await update.message.chat.send_action("typing")
    try:
        results = await search_stations(text)
    except Exception as e:
        logger.error("Recherche : %s", type(e).__name__)
        await update.message.reply_text(api_error(e))
        return

    if not results:
        await update.message.reply_text(
            f"Aucune gare pour « {html.escape(text)} ».\n<i>Essaie la ville seule.</i>",
            parse_mode=ParseMode.HTML)
        return
    if len(results) == 1:
        await ask_day(update.message, results[0]["id"], edit=False)
        return

    await update.message.reply_text(
        "Quelle gare ?",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(c["name"][:60], callback_data=f"j|{c['id']}")]
             for c in results]))


HORIZON = 180   # jours : au-delà, les horaires ne sont pas encore publiés


def parse_day(text: str) -> date_cls | None:
    """Accepte JJ/MM et JJ/MM/AAAA. Une date déjà passée bascule sur l'an prochain."""
    txt = text.strip().replace(".", "/").replace("-", "/")
    now = now_local().date()
    for fmt, has_year in (("%d/%m/%Y", True), ("%d/%m/%y", True), ("%d/%m", False)):
        try:
            d = datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
        if not has_year:
            d = d.replace(year=now.year)
            if d < now:
                d = d.replace(year=now.year + 1)
        return d
    return None


async def ask_day(target, sid: str, edit: bool):
    await say(target, f"🚉 <b>{html.escape(_names.get(sid, 'Gare'))}</b>\nQuel jour ?",
              edit, parse_mode=ParseMode.HTML, reply_markup=day_keyboard(sid))


# ── Boutons ───────────────────────────────────────────────────────────────────

async def on_button(update: Update, ctx):
    if not allowed(update):
        return
    q = update.callback_query
    uid = update.effective_user.id
    parts = q.data.split("|")
    a = parts[0]

    if rate_limited(uid):
        await q.answer("Trop de requêtes, patiente un instant.", show_alert=True)
        return

    # Choix du jour
    if a == "j":
        await q.answer()
        await ask_day(q, "|".join(parts[1:]), edit=True)
        return

    if a == "o":
        await q.answer()
        sid = "|".join(parts[1:])
        ctx.user_data["await_date"] = sid
        await say(q, "Quelle date ? Envoie-la au format <b>JJ/MM</b>.", True,
                  parse_mode=ParseMode.HTML)
        return

    # Tableau des départs
    if a == "b":
        from_dt, sid = parts[1], "|".join(parts[2:])
        await q.answer(await show_board(q, uid, sid, from_dt, edit=True))
        return

    # Fiche d'un train
    if a == "t":
        await q.answer()
        sched, num, sid = parts[1], parts[2], "|".join(parts[3:])
        from_dt = (parse_dt(sched) - timedelta(minutes=20)).strftime("%Y%m%dT%H%M%S")
        try:
            items, _ = await cached_departures(sid, from_dt, count=20)
        except Exception as e:
            await say(q, api_error(e), True)
            return
        rows = parse_rows(items)
        live = next((r for r in rows if r["num"] == num and r["base"] == parse_dt(sched)), None)
        if not live:
            live = next((r for r in rows if r["num"] == num), None)

        key = watch_key(sid, sched, num)
        w = get_watches(uid).get(key) or {
            "sid": sid, "station": _names.get(sid, "Gare"), "num": num,
            "sched": sched, "mode": live["mode"] if live else "Train",
            "dir": live["dir"] if live else "", "chat_id": q.message.chat_id,
        }
        ctx.user_data["pending"] = {key: w}
        back = parts[1]
        await say(q, format_train(w, live), True, parse_mode=ParseMode.HTML,
                  reply_markup=train_keyboard(key, key in get_watches(uid), sid,
                                              start_of(parse_dt(sched).date())))
        return

    # Suivre
    if a == "w":
        key = parts[1]
        if not ctx.job_queue:
            await q.answer("Suivi indisponible sur cette instance.", show_alert=True)
            return
        watches = get_watches(uid)
        if key in watches:
            await q.answer("Déjà suivi.")
            return
        if len(watches) >= MAX_WATCHES:
            await q.answer(f"Maximum {MAX_WATCHES} trains suivis.", show_alert=True)
            return
        w = (ctx.user_data.get("pending") or {}).get(key)
        if not w:
            await q.answer("Fiche expirée, rouvre le train.", show_alert=True)
            return
        w["chat_id"] = q.message.chat_id
        watches[key] = w
        user_update(uid, watches=watches)
        schedule_watch(ctx.job_queue, uid, key)

        sched = parse_dt(w["sched"])
        await q.answer("Suivi activé 🔔")
        await say(q, format_train(w, None if w.get("gone") else
                                  {"delay": w.get("delay", 0), "real": sched}),
                  True, parse_mode=ParseMode.HTML,
                  reply_markup=train_keyboard(key, True, w["sid"], start_of(sched.date())))
        await q.message.chat.send_message(
            f"🔔 Je surveille le <b>{html.escape(w['mode'])} {html.escape(w['num'])}</b>.\n"
            f"Je te préviens à partir de {WATCH_FROM} min avant le départ, "
            "et à chaque changement.", parse_mode=ParseMode.HTML)
        return

    # Ne plus suivre
    if a == "u":
        key = parts[1]
        watches = get_watches(uid)
        w = watches.pop(key, None)
        user_update(uid, watches=watches)
        if ctx.job_queue:
            cancel_watch(ctx.job_queue, uid, key)
        await q.answer("Suivi arrêté")
        if w:
            sched = parse_dt(w["sched"])
            await say(q, format_train(w, {"delay": w.get("delay", 0), "real": sched}), True,
                      parse_mode=ParseMode.HTML,
                      reply_markup=train_keyboard(key, False, w["sid"], start_of(sched.date())))
        return


# ── Démarrage ─────────────────────────────────────────────────────────────────

async def on_start(app: Application):
    await app.bot.set_my_commands([
        BotCommand("suivis", "Trains que je surveille"),
        BotCommand("start", "Chercher une gare"),
    ])
    # Les suivis survivent au redémarrage : sans cela, une alerte promise
    # disparaîtrait au premier `docker compose up`.
    if not app.job_queue:
        logger.warning("job_queue absente : les alertes sont désactivées.")
        return
    restored = 0
    for uid_s, entry in load_state().items():
        for key, w in (entry.get("watches") or {}).items():
            sched = parse_dt(w.get("sched", ""))
            if sched and (sched - now_local()).total_seconds() / 60 > -WATCH_UNTIL:
                schedule_watch(app.job_queue, int(uid_s), key)
                restored += 1
    logger.info("Suivis restaurés : %d", restored)


def main():
    token = read_secret("TELEGRAM_TOKEN")
    logger.info("Allowlist : %s",
                f"{len(ALLOWED_IDS)} utilisateurs" if ALLOWED_IDS else "désactivée")

    app = Application.builder().token(token).build()
    app.add_handlers([
        CommandHandler("start", start_command),
        CommandHandler("help", start_command),
        CommandHandler("suivis", watches_command),
        CallbackQueryHandler(on_button),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
    ])
    app.post_init = on_start
    logger.info("Bot démarré.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

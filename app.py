"""
SenpaiX v5 — réseau social otaku, look "Otakun Mobile".
Flask + Postgres (Render/Neon) ou SQLite (local). Sources : AniList + Jikan.
Local :  pip install flask && python app.py        ->  http://localhost:5000
Render : fichier autonome — remplace simplement app.py, rien d'autre à modifier.
  Seule variable utile : DATABASE_URL (adresse Postgres Neon) -> données conservées entre les mises à jour.
  Facultatif : SECRET_KEY (sinon dérivée de DATABASE_URL), ADMIN_USERNAME / ADMIN_PASSWORD
  (sinon crée l'administrateur sur la page /installation), SENPAIX_DEBUG=1 (détails dans /healthz?db=1).
  Le pilote Postgres (psycopg2) s'installe tout seul s'il manque ; gunicorn n'est pas obligatoire.
"""
import os, io, re, json, time, random, sqlite3, secrets, logging, threading
import urllib.request, urllib.parse, urllib.error, ssl
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import (Flask, request, redirect, session, render_template, flash, abort, g,
                   jsonify, url_for, send_file)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "senpaix.db")
KEY_FILE = os.path.join(BASE, ".secret_key")
CREATOR = os.environ.get("SENPAIX_CREATOR", "Seny Alseny Touré")
CREATOR_BIO = ("Jeune créateur guinéen passionné de mangas, d'animés et de développement web. "
               "SenpaiX offre à la communauté otaku un espace chaleureux, sécurisé et sans publicité.")
EMAIL = os.environ.get("SENPAIX_CONTACT_EMAIL", "toureseniba8@gmail.com")
WA = os.environ.get("SENPAIX_WHATSAPP", "224621570861")
WA_SHOW = os.environ.get("SENPAIX_WHATSAPP_DISPLAY", "+224 621 57 08 61")
FB = os.environ.get("SENPAIX_FACEBOOK", "https://www.facebook.com/share/1DqtuuwwrJ/")
JIKAN = "https://api.jikan.moe/v4"
ANILIST = "https://graphql.anilist.co"
TOURNAMENT_HOUR_UTC = 18          # tournoi quotidien à 18:00 UTC
Q_COUNT, Q_TIME = 10, 15
RINGS = {"none": "Aucun", "flame": "Flamme 🔥", "neon": "Néon 💡", "rainbow": "Arc-en-ciel 🌈", "sakura": "Sakura 🌸", "ice": "Glace ❄️"}
FXS = {"none": "Aucun", "sakura": "Pétales 🌸", "stars": "Étoiles ✨", "snow": "Neige ❄️", "flames": "Flammes 🔥"}
NAMES = {"normal": "Normal", "flame": "Flamme", "neon": "Néon", "rainbow": "Arc-en-ciel"}
BANNERS = {"sunset": "Coucher de soleil", "ocean": "Océan", "sakura": "Sakura", "night": "Nuit", "forest": "Forêt", "cyber": "Cyber"}
RESERVED = {"modifier", "admin", "administrateur", "moderateur", "modérateur", "senpaix", "support", "system", "root", "null", "undefined"}
STATUSES = {"cours": "En cours", "voir": "À voir", "fini": "Terminé"}
REASONS = ["Spam ou arnaque", "Harcèlement ou insultes", "Spoiler non signalé", "Contenu inapproprié", "Autre"]
GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Horror", "Mahou Shoujo", "Mecha", "Music", "Mystery", "Psychological", "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller"]
SORTS = {"POPULARITY_DESC": "Popularité", "SCORE_DESC": "Meilleure note", "TRENDING_DESC": "Tendance", "START_DATE_DESC": "Plus récents"}          # 10 questions, 15 s

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("senpaix")


_m = re.search(r"postgres(?:ql)?://[^\s'\"]+", os.environ.get("DATABASE_URL", ""))
DATABASE_URL = _m.group(0).replace("postgres://", "postgresql://", 1) if _m else ""


def secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if DATABASE_URL:      # stable entre les redémarrages et secrète (contient le mot de passe de la base)
        import hashlib
        return hashlib.sha256(("senpaix-session-v1|" + DATABASE_URL).encode()).hexdigest()
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f:
            f.write(secrets.token_hex(32))
    return open(KEY_FILE).read().strip()


PROD = bool(os.environ.get("RENDER")) or os.environ.get("SENPAIX_PROD") == "1"
if PROD and not os.environ.get("SECRET_KEY") and not DATABASE_URL:
    log.error("SECRET_KEY absente : les utilisateurs seront déconnectés à chaque redémarrage. Ajoute-la dans Render > Environment.")
app = Flask(__name__)
app.secret_key = secret()
if PROD:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PROD,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30, MAX_CONTENT_LENGTH=10 * 1024 * 1024)

# ───────────────────────────── Base de données ─────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,pw TEXT NOT NULL,
 bio TEXT DEFAULT '',theme TEXT DEFAULT 'dark',notify INTEGER DEFAULT 1,privacy TEXT DEFAULT 'public',
 dm TEXT DEFAULT 'all',sv INTEGER DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 body TEXT NOT NULL,mal_id INTEGER,m_title TEXT,m_img TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS likes(user_id INTEGER,post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,PRIMARY KEY(user_id,post_id));
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY,post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
 user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS communities(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,topic TEXT,descr TEXT,
 creator_id INTEGER REFERENCES users(id) ON DELETE CASCADE,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(cid INTEGER REFERENCES communities(id) ON DELETE CASCADE,
 user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,PRIMARY KEY(cid,user_id));
CREATE TABLE IF NOT EXISTS cposts(id INTEGER PRIMARY KEY,cid INTEGER REFERENCES communities(id) ON DELETE CASCADE,
 user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cmedia(id INTEGER PRIMARY KEY,pid INTEGER REFERENCES cposts(id) ON DELETE CASCADE,mime TEXT,data BLOB,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS creacts(pid INTEGER REFERENCES cposts(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,emo TEXT,PRIMARY KEY(pid,user_id));
CREATE INDEX IF NOT EXISTS cmedia_pid ON cmedia(pid);
CREATE INDEX IF NOT EXISTS cposts_cid ON cposts(cid,id);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT DEFAULT 'evenement',title TEXT NOT NULL,body TEXT DEFAULT '',link TEXT DEFAULT '',day TEXT DEFAULT '',fin TEXT DEFAULT '',pos INTEGER DEFAULT 0,hidden INTEGER DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS eimg(eid INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,data BLOB);
CREATE TABLE IF NOT EXISTS downloads(id INTEGER PRIMARY KEY,title TEXT NOT NULL,url TEXT NOT NULL,body TEXT DEFAULT '',plat TEXT DEFAULT 'autre',version TEXT DEFAULT '',size TEXT DEFAULT '',hits INTEGER DEFAULT 0,pos INTEGER DEFAULT 0,hidden INTEGER DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dicon(did INTEGER PRIMARY KEY REFERENCES downloads(id) ON DELETE CASCADE,data BLOB);
CREATE TABLE IF NOT EXISTS trailers(id INTEGER PRIMARY KEY,title TEXT NOT NULL,yt TEXT NOT NULL,mal_id INTEGER DEFAULT 0,stitle TEXT DEFAULT '',pos INTEGER DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gseen(cid INTEGER REFERENCES communities(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,seen INTEGER DEFAULT 0,mute INTEGER DEFAULT 0,PRIMARY KEY(cid,user_id));
CREATE TABLE IF NOT EXISTS gkick(cid INTEGER REFERENCES communities(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,created TEXT,PRIMARY KEY(cid,user_id));
CREATE TABLE IF NOT EXISTS pmedia(id INTEGER PRIMARY KEY,pid INTEGER REFERENCES posts(id) ON DELETE CASCADE,mime TEXT,data BLOB,created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pmedia_pid ON pmedia(pid);
CREATE TABLE IF NOT EXISTS popts(id INTEGER PRIMARY KEY,post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,label TEXT NOT NULL,pos INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pvotes(post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,opt_id INTEGER REFERENCES popts(id) ON DELETE CASCADE,PRIMARY KEY(post_id,user_id));
CREATE TABLE IF NOT EXISTS cimg(cid INTEGER REFERENCES communities(id) ON DELETE CASCADE,kind TEXT NOT NULL,data BLOB,PRIMARY KEY(cid,kind));
CREATE TABLE IF NOT EXISTS favs(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,mal_id INTEGER,title TEXT,img TEXT,
 PRIMARY KEY(user_id,mal_id));
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
 mode TEXT,good INTEGER,total INTEGER,ms INTEGER,day TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocks(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,blocked_id INTEGER REFERENCES users(id) ON DELETE CASCADE,PRIMARY KEY(user_id,blocked_id));
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,reporter_id INTEGER REFERENCES users(id) ON DELETE CASCADE,kind TEXT,target_id INTEGER,reason TEXT,status TEXT DEFAULT 'ouvert',created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY,admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE,action TEXT,target TEXT,detail TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS images(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,kind TEXT,data BLOB,PRIMARY KEY(user_id,kind));
CREATE TABLE IF NOT EXISTS notifs(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
 text TEXT,link TEXT DEFAULT '/',read INTEGER DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY,s INTEGER REFERENCES users(id) ON DELETE CASCADE,
 r INTEGER REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,read INTEGER DEFAULT 0,created TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


PG = DATABASE_URL.startswith("postgresql")


def install_psycopg2():
    """Installe le pilote Postgres si requirements.txt ne le contient pas (aucun fichier à modifier)."""
    import subprocess, sys, importlib
    target = os.path.join(BASE, ".pylibs")
    log.warning("psycopg2 absent : installation automatique (une seule fois par démarrage)…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--default-timeout", "20",
                           "--target", target, "psycopg2-binary"], timeout=50)
    sys.path.insert(0, target)
    importlib.invalidate_caches()


if PG:
    import sys
    if os.path.isdir(os.path.join(BASE, ".pylibs")):
        sys.path.insert(0, os.path.join(BASE, ".pylibs"))
    try:
        import psycopg2, psycopg2.extras
    except ImportError:
        try:
            install_psycopg2()
            import psycopg2, psycopg2.extras
        except Exception:
            log.exception("Impossible d'installer psycopg2 : retour au stockage temporaire SQLite")
            PG = False
if PG:
    DBError = psycopg2.Error
else:
    DBError = sqlite3.Error
DURABLE = PG or not PROD      # sur Render sans Postgres, SQLite est effacé à chaque mise à jour
ID_TABLES = {"users", "posts", "comments", "communities", "cposts", "cmedia", "pmedia", "trailers", "events", "downloads", "scores", "notifs", "msgs", "reports", "admin_log"}
FAV_UPSERT = (" ON CONFLICT (user_id,mal_id) DO UPDATE SET title=EXCLUDED.title,img=EXCLUDED.img,status=EXCLUDED.status,ep=EXCLUDED.ep,"
              "total=EXCLUDED.total,score=EXCLUDED.score,kind=EXCLUDED.kind,updated=EXCLUDED.updated")
MIGRATIONS = (("users", "email TEXT DEFAULT ''"), ("users", "recovery TEXT DEFAULT ''"), ("users", "banned INTEGER DEFAULT 0"),
              ("users", "is_admin INTEGER DEFAULT 0"), ("users", "ban_reason TEXT DEFAULT ''"), ("users", "av INTEGER DEFAULT 0"), ("users", "bv INTEGER DEFAULT 0"),
              ("users", "p_ring TEXT DEFAULT 'none'"), ("users", "p_fx TEXT DEFAULT 'none'"), ("users", "p_name TEXT DEFAULT 'normal'"),
              ("users", "p_banner TEXT DEFAULT 'sunset'"), ("users", "p_color TEXT DEFAULT '#ff6a1a'"), ("users", "p_status TEXT DEFAULT ''"),
              ("cposts", "mk TEXT DEFAULT ''"), ("cposts", "rid INTEGER DEFAULT 0"), ("cposts", "mal_id INTEGER DEFAULT 0"), ("cposts", "m_title TEXT DEFAULT ''"), ("cposts", "m_img TEXT DEFAULT ''"), ("communities", "pin INTEGER DEFAULT 0"), ("posts", "has_img INTEGER DEFAULT 0"), ("users", "otype TEXT DEFAULT ''"), ("communities", "av INTEGER DEFAULT 0"), ("communities", "cv INTEGER DEFAULT 0"), ("favs", "status TEXT DEFAULT 'voir'"), ("favs", "ep INTEGER DEFAULT 0"), ("favs", "total INTEGER DEFAULT 0"),
              ("favs", "score INTEGER DEFAULT 0"), ("favs", "kind TEXT DEFAULT 'anime'"), ("favs", "updated TEXT DEFAULT ''"))


def pg_sql(sql):
    sql = sql.replace("%", "%%").replace("?", "%s")
    if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO FAVS"):
        sql = re.sub(r"INSERT OR REPLACE INTO", "INSERT INTO", sql, 1, flags=re.I) + FAV_UPSERT
    return re.sub(r"\bLIKE\b", "ILIKE", sql)


class Res:
    def __init__(self, c, lid=None):
        self.c, self.lastrowid, self.rowcount = c, lid, c.rowcount

    def fetchone(self):
        return self.c.fetchone()

    def fetchall(self):
        return self.c.fetchall()

    def __iter__(self):
        return iter(self.c.fetchall())


class Conn:
    def __init__(self):
        if PG:
            self.con = psycopg2.connect(DATABASE_URL, connect_timeout=15, cursor_factory=psycopg2.extras.DictCursor)
        else:
            self.con = sqlite3.connect(DB_PATH)
            self.con.row_factory = sqlite3.Row
            self.con.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, args=()):
        if not PG:
            c = self.con.execute(sql, args)
            return Res(c, c.lastrowid)
        sql = pg_sql(sql)
        m = re.match(r"\s*INSERT INTO (\w+)", sql, re.I)
        ret = bool(m and m.group(1).lower() in ID_TABLES)
        c = self.con.cursor()
        args = tuple(psycopg2.Binary(a) if isinstance(a, (bytes, bytearray)) else a for a in args)
        c.execute(sql + (" RETURNING id" if ret else ""), args)
        return Res(c, c.fetchone()[0] if ret else None)

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()


def db():
    if "db" not in g:
        try:
            g.db = Conn()
        except DBError:                     # base Neon qui se réveille : 2e essai
            time.sleep(1)
            g.db = Conn()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d:
        try:
            d.rollback() if exc else d.commit()
        except Exception:
            log.exception("commit/rollback")
        d.close()


def rowd(r):
    return {k: r[k] for k in r.keys()}


# Les membres déjà présents avant l'arrivée des alertes de groupe n'ont pas de « dernier message vu » : on les met à jour (rien ne s'affiche comme non lu).
BACKFILL_SEEN = ("INSERT INTO gseen(cid,user_id,seen) SELECT m.cid,m.user_id,COALESCE((SELECT MAX(id) FROM cposts WHERE cid=m.cid),0) FROM members m "
                 "WHERE NOT EXISTS (SELECT 1 FROM gseen s WHERE s.cid=m.cid AND s.user_id=m.user_id)")


def init_db():
    con = Conn()
    try:
        if PG:
            c = con.con.cursor()
            c.execute("SELECT pg_advisory_xact_lock(727001)")
            for st in [x.strip() for x in SCHEMA.replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY").replace("BLOB", "BYTEA").split(";") if x.strip()]:
                c.execute(st)
            for t, col in MIGRATIONS:
                c.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s" % (t, col))
            c.execute(BACKFILL_SEEN)
            con.commit()
        else:
            con.con.executescript(SCHEMA)
            for t, col in MIGRATIONS:
                try:
                    con.con.execute("ALTER TABLE %s ADD COLUMN %s" % (t, col))
                except sqlite3.OperationalError:
                    pass
            con.con.execute(BACKFILL_SEEN)
            con.commit()
    finally:
        con.close()


def next_tournament():
    n = datetime.now(timezone.utc)
    t = n.replace(hour=TOURNAMENT_HOUR_UTC, minute=0, second=0, microsecond=0)
    if t <= n:
        t += timedelta(days=1)
    return int((t - n).total_seconds())


# ───────────────────────────── Helpers ─────────────────────────────
def safe_rollback():
    d = g.get("db")
    if d:
        try:
            d.rollback()
        except Exception:
            pass


def me():
    if "me" not in g:
        g.me = None
        uid = session.get("uid")
        if uid:
            try:
                u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            except DBError:
                log.exception("me() : base injoignable")
                safe_rollback()
                return g.me
            if u and u["sv"] == session.get("sv") and not u["banned"]:
                g.me = u
            else:
                session.clear()
    return g.me


COMMON_PW = {"password", "motdepasse", "12345678", "123456789", "1234567890", "azerty123", "azertyuiop", "qwerty123", "qwertyuiop", "motdepasse1", "motdepasse123",
             "password1", "password123", "11111111", "00000000", "iloveyou", "senpaix", "senpaix123", "naruto123", "anime123", "goku1234", "azerty1234", "abcd1234"}


def pw_problem(p, username=""):
    if not 8 <= len(p) <= 128:
        return "Mot de passe : 8 caractères minimum."
    if p.lower() in COMMON_PW or len(set(p)) < 4:
        return "Ce mot de passe est trop courant : choisis-en un plus original."
    if username and len(username) >= 3 and username.lower() in p.lower():
        return "Le mot de passe ne doit pas contenir ton pseudo."
    return ""


def user_is_admin(u):
    return bool(u) and (bool(u["is_admin"]) or u["username"] in os.environ.get("SENPAIX_ADMINS", "").split(","))


def is_admin():
    return user_is_admin(me())


ADM = {"yes": False, "t": 0.0}


def admin_exists(fresh=False):
    if ADM["yes"]:
        return True
    if not fresh and time.time() - ADM["t"] < 30:
        return False
    ADM["t"] = time.time()
    try:
        ADM["yes"] = bool(os.environ.get("SENPAIX_ADMINS")) or db().execute("SELECT 1 FROM users WHERE is_admin=1").fetchone() is not None
    except DBError:
        safe_rollback()
        return True            # en cas de doute on masque l'installation
    return ADM["yes"]


def admin_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if not me():
            return redirect("/connexion?next=" + urllib.parse.quote(request.path))
        if not is_admin():
            if not admin_exists(True):
                return redirect("/installation")
            abort(403)
        return fn(*a, **k)
    return w


def alog(action, target="", detail=""):
    db().execute("INSERT INTO admin_log(admin_id,action,target,detail,created) VALUES(?,?,?,?,?)", (uid(), action, str(target)[:80], str(detail)[:200], now()))


def toint(v, d=0, lo=0, hi=100000):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not me():
            flash("Connecte-toi d'abord.", "err")
            return redirect("/connexion?next=" + urllib.parse.quote(request.path))
        return fn(*a, **k)
    return w


def txt(v, n=2000):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (v or "").strip())[:n]


def notify(uid, text, link="/"):
    u = db().execute("SELECT notify FROM users WHERE id=?", (uid,)).fetchone()
    if u and u["notify"] and (not me() or uid != me()["id"]):
        db().execute("INSERT INTO notifs(user_id,text,link,created) VALUES(?,?,?,?)", (uid, text, link, now()))


def ago(v):
    try:
        s = (datetime.now(timezone.utc) - datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds()
    except Exception:
        return ""
    for lim, div, u in ((60, 1, "à l'instant"), (3600, 60, "min"), (86400, 3600, "h"), (2592000, 86400, "j")):
        if s < lim:
            return u if div == 1 else f"{int(s // div)} {u}"
    return v[:10]


PROFS = {"t": 0, "d": {}}


def profs():
    if time.time() - PROFS["t"] > 60:
        PROFS["t"] = time.time()
        try:
            PROFS["d"] = {r["username"]: (r["av"], r["p_ring"], r["p_color"]) for r in
                          db().execute("SELECT username,av,p_ring,p_color FROM users WHERE av>0 OR p_ring!='none'").fetchall()}
        except Exception:
            log.exception("profs")
            safe_rollback()
    return PROFS["d"]


def ava(name, size=40):
    v, ring, col = profs().get(name, (0, "none", "#ff6a1a"))
    if v:
        inner = f'<img class="ava" src="/u/{name}/avatar?v={v}" alt="" loading="lazy" style="width:{size}px;height:{size}px">'
    else:
        h = sum(map(ord, name)) % 360
        inner = (f'<span class="ava" style="width:{size}px;height:{size}px;font-size:{size // 2.3}px;'
                 f'background:linear-gradient(135deg,hsl({h} 80% 55%),hsl({(h + 50) % 360} 85% 45%))">{name[:1].upper()}</span>')
    return Markup(f'<span class="avw ring-{ring}" style="--c:{col}">{inner}</span>')


ICONS = {
    "home": "M3 11l9-8 9 8v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z",
    "users": "M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M10 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M21 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
    "game": "M6 12h4M8 10v4M15 13h.01M18 11h.01M17.3 5H6.7a4 4 0 0 0-3.9 3.2L1.5 15a3 3 0 0 0 5 2.6L8 16h8l1.5 1.6a3 3 0 0 0 5-2.6l-1.3-6.8A4 4 0 0 0 17.3 5z",
    "compass": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM16 8l-2 6-6 2 2-6z",
    "user": "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8",
    "bell": "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0",
    "search": "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
    "chev": "M9 18l6-6-6-6", "back": "M15 18l-6-6 6-6",
    "msg": "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    "heart": "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z",
    "sliders": "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
    "lock": "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
    "shield": "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    "mail": "M3 5h18v14H3zM3 5l9 8 9-8",
    "info": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
    "out": "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
    "trash": "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6", "dl": "M21 15v4H3v-4M7 10l5 5 5-5M12 15V3",
    "dots": "M12 5h.01M12 12h.01M12 19h.01",
    "image": "M3 5h18v14H3zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 15l-5-5L5 19",
    "video": "M2 6h13v12H2zM15 10l7-4v12l-7-4",
    "plus": "M12 5v14M5 12h14", "reply": "M9 14L4 9l5-5M20 20v-7a4 4 0 0 0-4-4H4", "tv": "M2 7h20v13H2zM17 2l-5 5-5-5",
    "pin": "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0zM12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "chart": "M4 20V10M10 20V4M16 20v-8M22 20H2", "edit": "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
    "mic": "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v1a7 7 0 0 1-14 0v-1M12 18v4M8 22h8",
    "smile": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01", "send": "M22 2L11 13M22 2l-7 20-4-9-9-4z", "star": "M12 2l3 7 7 .6-5.3 4.8 1.6 7.4L12 18l-6.3 3.8 1.6-7.4L2 9.6 9 9z",
}


def ic(name, size=22):
    return Markup(f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="{ICONS[name]}"/></svg>')


@app.context_processor
def inject():
    u = me()
    unread, gunread = 0, 0
    if u:
        try:
            unread = db().execute("SELECT (SELECT COUNT(*) FROM notifs WHERE user_id=? AND read=0)+"
                                  "(SELECT COUNT(*) FROM msgs WHERE r=? AND read=0)", (u["id"], u["id"])).fetchone()[0]
            gunread = sum(x for x, mu in group_unread(db()).values() if not mu)
        except DBError:
            log.exception("inject : compteur de notifications")
            safe_rollback()
    return dict(setup_open=bool(u) and not admin_exists(), durable=DURABLE, STATUSES=STATUSES, REASONS=REASONS, is_admin=is_admin(), me=u, ic=ic, ava=ava, ago=ago, unread=unread, gunread=gunread, path=request.path,
                csrf=lambda: Markup(f'<input type="hidden" name="_csrf" value="{session.setdefault("csrf", secrets.token_hex(16))}">'))


@app.before_request
def small_posts():
    """Seuls les envois de médias de groupe peuvent dépasser 256 Ko."""
    if request.method == "POST" and (request.content_length or 0) > 256 * 1024 \
            and request.path not in ("/api/publier", "/api/admin/evenement") and not (request.path.startswith("/api/groupe/") and request.path.endswith("/envoi")):
        abort(413)


@app.before_request
def csrf_guard():
    if request.method == "POST":
        if request.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("X-CSRF", ""), session.get("csrf", "x")):
                abort(400)
        elif not secrets.compare_digest(request.form.get("_csrf", ""), session.get("csrf", "x")):
            abort(400)


RATE = {}


def throttle(key, limit, window):
    t = time.time()
    if len(RATE) > 4000:
        RATE.clear()
    RATE[key] = [x for x in RATE.get(key, []) if t - x < window]
    if len(RATE[key]) >= limit:
        return False
    RATE[key].append(t)
    return True


POSTQ = ("SELECT p.*,u.username,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) lc,"
         "(SELECT COUNT(*) FROM comments WHERE post_id=p.id) cc,(SELECT COUNT(*) FROM popts WHERE post_id=p.id) np,"
         "(SELECT COUNT(*) FROM likes WHERE post_id=p.id AND user_id=?) liked FROM posts p JOIN users u ON u.id=p.user_id ")


def uid():
    return me()["id"] if me() else 0


# ───────────────────────────── Accueil / Découverte ─────────────────────────────
@app.route("/")
def index():
    tab = request.args.get("t", "decouverte")
    conds, args = ["p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?)"], [uid(), uid()]
    if tab == "suivis" and me():
        conds.append("p.mal_id IN (SELECT mal_id FROM favs WHERE user_id=?)")
        args.append(uid())
    posts = db().execute(POSTQ + "WHERE " + " AND ".join(conds) + " ORDER BY p.id DESC LIMIT 40", args).fetchall()
    watch = [r[0] for r in db().execute("SELECT mal_id FROM favs WHERE user_id=? AND status IN('cours','voir') AND kind='anime' ORDER BY updated DESC LIMIT 50", (uid(),))] if me() else []
    trailers = db().execute("SELECT * FROM trailers ORDER BY pos DESC,id DESC LIMIT 8").fetchall()
    return render_template("home.html", title="Découverte", tab=tab, posts=posts, eta=next_tournament(), watch=watch, trailers=trailers, events=evt_live(db(), 6), dlcount=db().execute("SELECT COUNT(*) FROM downloads WHERE hidden=0").fetchone()[0])


YT_RE = re.compile(r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/|v/))([\w-]{11})(?![\w-])")


def yt_id(v):
    """Identifiant YouTube (11 caractères) à partir d'un lien collé ou de l'identifiant seul ; None si invalide."""
    v = (v or "").strip()
    if re.fullmatch(r"[\w-]{11}", v):
        return v
    m = YT_RE.search(v)
    return m.group(1) if m else None


EVT_KINDS = {"evenement": "🎉 Événement", "jeu": "🎮 Jeu", "annonce": "📢 Annonce"}
FR_MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def fdate(v):
    try:
        y, m, d = (int(x) for x in (v or "")[:10].split("-"))
        return f"{d} {FR_MOIS[m - 1]} {y}"
    except Exception:
        return ""


def clean_link(v):
    """Lien du bouton : chemin interne (/quiz) ou adresse https:// ; '' si vide ; None si invalide (jamais javascript:, http:, //…)."""
    v = (v or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"/(?![/\\])[\w\-./?=&%#]{0,200}", v) or re.fullmatch(r"https://[^\s<>\"'\\]{4,280}", v):
        return v
    return None


def clean_day(v):
    v = (v or "").strip()
    return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) and fdate(v) else ""


EVT_COLS = "id,kind,title,body,link,day,fin,created"


def evt_live(d, limit=6):
    """Événements visibles : non masqués et pas encore terminés."""
    return d.execute(f"SELECT {EVT_COLS} FROM events WHERE hidden=0 AND (fin='' OR fin>=?) ORDER BY pos DESC,id DESC LIMIT ?", (now()[:10], limit)).fetchall()


@app.route("/evt/<int:i>")
def evt_img(i):
    r = db().execute("SELECT data FROM eimg WHERE eid=?", (i,)).fetchone() or abort(404)
    resp = app.response_class(bytes(r["data"]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/evenements")
def events_page():
    d = db()
    past = d.execute(f"SELECT {EVT_COLS} FROM events WHERE hidden=0 AND fin<>'' AND fin<? ORDER BY fin DESC LIMIT 12", (now()[:10],)).fetchall()
    return render_template("events.html", title="À la une", rows=evt_live(d, 50), past=past)


@app.route("/evenement/<int:i>")
def event_page(i):
    e = db().execute(f"SELECT {EVT_COLS},hidden FROM events WHERE id=?", (i,)).fetchone() or abort(404)
    if e["hidden"] and not is_admin():
        abort(404)
    return render_template("event.html", title=e["title"], e=e, over=bool(e["fin"] and e["fin"] < now()[:10]))


@app.route("/admin/a-la-une")
@admin_only
def admin_events():
    rows = [rowd(r) for r in db().execute("SELECT id,kind,title,link,day,fin,hidden,created FROM events ORDER BY pos DESC,id DESC").fetchall()]
    today = now()[:10]
    for r in rows:
        r["st"] = "Masqué" if r["hidden"] else ("Terminé" if r["fin"] and r["fin"] < today else "En ligne")
    return render_template("admin_events.html", title="À la une", tab="events", rows=rows)


@app.route("/api/admin/evenement", methods=["POST"])
def api_evt_add():
    """Ajout d'un événement / jeu / annonce : corps brut = image JPEG (redimensionnée par la page), le reste dans l'URL."""
    if not is_admin():
        return jsonify(error="Réservé aux administrateurs."), 403
    d, a = db(), request.args
    data = request.get_data(cache=False)
    if data[:3] != b"\xff\xd8\xff" or not 500 < len(data) <= 700_000:
        return jsonify(error="Image invalide (JPEG, 700 Ko maximum)."), 400
    title, body, link = txt(a.get("t"), 80), txt(a.get("d"), 600), clean_link(a.get("l"))
    day, fin = clean_day(a.get("dt")), clean_day(a.get("fin"))
    if not title:
        return jsonify(error="Donne un titre."), 400
    if link is None:
        return jsonify(error="Lien invalide : utilise un chemin comme /quiz ou une adresse https://…"), 400
    if day and fin and fin < day:
        return jsonify(error="La date de fin est avant la date de début."), 400
    if d.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= 40:
        return jsonify(error="Maximum 40 éléments : supprime-en un d'abord."), 400
    kind = a.get("c") if a.get("c") in EVT_KINDS else "evenement"
    top = d.execute("SELECT COALESCE(MAX(pos),0) FROM events").fetchone()[0]
    eid = d.execute("INSERT INTO events(kind,title,body,link,day,fin,pos,created) VALUES(?,?,?,?,?,?,?,?)", (kind, title, body, link, day, fin, top + 1, now())).lastrowid
    d.execute("INSERT INTO eimg(eid,data) VALUES(?,?)", (eid, data))
    alog("ajoute à la une", title, kind)
    flash("Publié ✨ C'est visible en haut de l'accueil.", "ok")
    return jsonify(ok=True, id=eid)


PLATS = {"android": "🤖 Android", "ios": "🍎 iPhone / iPad", "pc": "💻 PC / Mac", "web": "🌐 Site web", "autre": "📎 Autre"}


def dl_url(v):
    """Lien de téléchargement : uniquement https:// ; None si invalide."""
    v = (v or "").strip()
    return v if re.fullmatch(r"https://[^\s<>\"'\\]{4,500}", v) else None


def host_of(u):
    try:
        return (urllib.parse.urlparse(u).hostname or "").removeprefix("www.")
    except Exception:
        return ""


DL_COLS = "d.id,d.title,d.url,d.body,d.plat,d.version,d.size,d.hits,d.hidden,d.created,(SELECT COUNT(*) FROM dicon WHERE did=d.id) has_icon"


@app.route("/dl/<int:i>/icone")
def dl_icon(i):
    r = db().execute("SELECT data FROM dicon WHERE did=?", (i,)).fetchone() or abort(404)
    resp = app.response_class(bytes(r["data"]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/telechargements")
def downloads_page():
    rows = db().execute(f"SELECT {DL_COLS} FROM downloads d WHERE d.hidden=0 ORDER BY d.pos DESC,d.id DESC LIMIT 100").fetchall()
    return render_template("downloads.html", title="Téléchargements", rows=rows)


@app.route("/telecharger/<int:i>")
def dl_go(i):
    """Redirige vers le lien enregistré par l'administrateur et compte le clic."""
    d = db()
    r = d.execute("SELECT url,hidden FROM downloads WHERE id=?", (i,)).fetchone() or abort(404)
    if r["hidden"] and not is_admin():
        abort(404)
    d.execute("UPDATE downloads SET hits=hits+1 WHERE id=?", (i,))
    resp = redirect(r["url"])
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.route("/admin/telechargements")
@admin_only
def admin_downloads():
    rows = db().execute(f"SELECT {DL_COLS} FROM downloads d ORDER BY d.pos DESC,d.id DESC").fetchall()
    return render_template("admin_downloads.html", title="Téléchargements", tab="downloads", rows=rows)


@app.route("/api/admin/telechargement", methods=["POST"])
def api_dl_add():
    """Ajout d'un lien de téléchargement : corps brut = icône JPEG facultative, le reste dans l'URL."""
    if not is_admin():
        return jsonify(error="Réservé aux administrateurs."), 403
    d, a = db(), request.args
    data = request.get_data(cache=False)
    if data and (data[:3] != b"\xff\xd8\xff" or len(data) > 120_000):
        return jsonify(error="Icône invalide (JPEG, 120 Ko maximum)."), 400
    title, url = txt(a.get("t"), 80), dl_url(a.get("u"))
    if not title:
        return jsonify(error="Donne un nom."), 400
    if not url:
        return jsonify(error="Lien invalide : il doit commencer par https:// (sans espace)."), 400
    if d.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] >= 60:
        return jsonify(error="Maximum 60 liens : supprime-en un d'abord."), 400
    plat = a.get("p") if a.get("p") in PLATS else "autre"
    top = d.execute("SELECT COALESCE(MAX(pos),0) FROM downloads").fetchone()[0]
    did = d.execute("INSERT INTO downloads(title,url,body,plat,version,size,pos,created) VALUES(?,?,?,?,?,?,?,?)",
                    (title, url, txt(a.get("d"), 600), plat, txt(a.get("v"), 20), txt(a.get("s"), 20), top + 1, now())).lastrowid
    if data:
        d.execute("INSERT INTO dicon(did,data) VALUES(?,?)", (did, data))
    alog("ajoute un téléchargement", title, host_of(url))
    flash("Lien ajouté 📥 Il est visible dans la page Téléchargements.", "ok")
    return jsonify(ok=True, id=did)


@app.route("/bandes-annonces")
def trailers_page():
    rows = db().execute("SELECT * FROM trailers ORDER BY pos DESC,id DESC LIMIT 100").fetchall()
    return render_template("trailers.html", title="Bandes-annonces", rows=rows)


@app.route("/recherche")
def search():
    qs = txt(request.args.get("q"), 60)
    us = db().execute("SELECT username,bio FROM users WHERE username LIKE ? AND privacy='public' LIMIT 12", (f"%{qs}%",)).fetchall() if qs else []
    return render_template("search.html", title="Recherche", q=qs, us=us)


def post_mal(src):
    """Série liée à une publication (depuis un formulaire ou une query) -> (mal_id, titre, image)."""
    mid = toint(src.get("mal_id"), 0, 0, 10 ** 9) or None
    t = txt(src.get("m_title"), 120) if mid else None
    img = src.get("m_img", "") if mid else None
    if img and not img.startswith("https://cdn.myanimelist.net/"):
        img = ""
    return mid, t, img


def clean_opts(lst):
    o = [x for x in (txt(v, 40) for v in lst) if x][:4]
    return o if len(o) >= 2 else []


def new_post(d, body, mid, t, img, opts, photo=None):
    pid = d.execute("INSERT INTO posts(user_id,body,mal_id,m_title,m_img,created,has_img) VALUES(?,?,?,?,?,?,?)",
                    (uid(), body, mid, t, img, now(), 1 if photo else 0)).lastrowid
    for i, o in enumerate(opts):
        d.execute("INSERT INTO popts(post_id,label,pos) VALUES(?,?,?)", (pid, o, i))
    if photo:
        d.execute("INSERT INTO pmedia(pid,mime,data,created) VALUES(?,?,?,?)", (pid, photo[0], photo[1], now()))
    return pid


@app.route("/publier", methods=["POST"])
@login_required
def publish():
    body = txt(request.form.get("body"), 1000)
    if not body or not throttle(f"pub{uid()}", 8, 300):
        flash("Message vide ou trop rapide.", "err")
        return redirect(request.referrer or "/")
    mid, t, img = post_mal(request.form)
    new_post(db(), body, mid, t, img, clean_opts(request.form.getlist("opt")))
    return redirect(request.referrer or "/")


@app.route("/api/publier", methods=["POST"])
def api_publish():
    """Publication avec photo : corps brut = image, le reste dans l'URL (t, o, mal_id…)."""
    if not me():
        return jsonify(error="Connecte-toi d'abord."), 401
    if (request.content_length or 0) > 1_600_000:
        return jsonify(error="Image trop lourde."), 413
    a = request.args
    body, data = txt(a.get("t"), 1000), request.get_data(cache=False)
    opts = clean_opts(a.getlist("o"))
    photo = None
    if data:
        mime, cap = sniff("image", data)
        if not mime:
            return jsonify(error="Image non reconnue."), 400
        if len(data) > cap:
            return jsonify(error="Image trop lourde."), 413
        photo = (mime, data)
    if not body and not photo:
        return jsonify(error="Écris quelque chose ou ajoute une photo."), 400
    if opts and not body:
        return jsonify(error="Écris la question de ton sondage."), 400
    if not throttle(f"pub{uid()}", 8, 300):
        return jsonify(error="Doucement ! Réessaie dans un instant."), 429
    mid, t, img = post_mal(a)
    return jsonify(ok=True, id=new_post(db(), body, mid, t, img, opts, photo))


def poll_of(pid):
    d = db()
    rows = d.execute("SELECT o.id,o.label,(SELECT COUNT(*) FROM pvotes v WHERE v.opt_id=o.id) n FROM popts o WHERE o.post_id=? ORDER BY o.pos,o.id", (pid,)).fetchall()
    mine = d.execute("SELECT opt_id FROM pvotes WHERE post_id=? AND user_id=?", (pid, uid())).fetchone() if me() else None
    tot = sum(r["n"] for r in rows)
    return dict(total=tot, mine=mine["opt_id"] if mine else 0,
                opts=[dict(id=r["id"], label=r["label"], n=r["n"], pct=round(100 * r["n"] / tot) if tot else 0) for r in rows])


@app.route("/api/sondage/<int:pid>/voter", methods=["POST"])
def api_vote(pid):
    if not me():
        return jsonify(error="Connecte-toi pour voter."), 401
    o = toint((request.get_json(silent=True) or {}).get("o"), 0, 0, 10 ** 12)
    d = db()
    if not d.execute("SELECT 1 FROM popts WHERE id=? AND post_id=?", (o, pid)).fetchone():
        return jsonify(error="Option inconnue."), 400
    if not throttle(f"pv{uid()}", 40, 300):
        return jsonify(error="Doucement !"), 429
    cur = d.execute("SELECT opt_id FROM pvotes WHERE post_id=? AND user_id=?", (pid, uid())).fetchone()
    d.execute("DELETE FROM pvotes WHERE post_id=? AND user_id=?", (pid, uid()))
    if not cur or cur["opt_id"] != o:     # retoucher la même option retire le vote
        d.execute("INSERT INTO pvotes(post_id,user_id,opt_id) VALUES(?,?,?)", (pid, uid(), o))
    return jsonify(ok=True, html=render_template("pollx.html", pid=pid))


@app.route("/aimer/<int:pid>", methods=["POST"])
@login_required
def like(pid):
    d = db()
    p = d.execute("SELECT user_id FROM posts WHERE id=?", (pid,)).fetchone() or abort(404)
    if d.execute("DELETE FROM likes WHERE user_id=? AND post_id=?", (uid(), pid)).rowcount == 0:
        d.execute("INSERT INTO likes VALUES(?,?)", (uid(), pid))
        notify(p["user_id"], f"{me()['username']} a aimé ton post", f"/post/{pid}")
    return redirect(request.referrer or "/")


@app.route("/post/<int:pid>", methods=["GET", "POST"])
def post_page(pid):
    d = db()
    p = d.execute(POSTQ + "WHERE p.id=?", (uid(), pid)).fetchone() or abort(404)
    if request.method == "POST":
        if not me():
            return redirect("/connexion")
        b = txt(request.form.get("body"), 500)
        if b and throttle(f"c{uid()}", 10, 300):
            d.execute("INSERT INTO comments(post_id,user_id,body,created) VALUES(?,?,?,?)", (pid, uid(), b, now()))
            notify(p["user_id"], f"{me()['username']} a commenté ton post", f"/post/{pid}")
        return redirect(f"/post/{pid}")
    cm = d.execute("SELECT c.*,u.username FROM comments c JOIN users u ON u.id=c.user_id WHERE post_id=? AND c.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?) ORDER BY c.id", (pid, uid())).fetchall()
    return render_template("post.html", title="Post", p=p, cm=cm)


@app.route("/supprimer/<kind>/<int:i>", methods=["POST"])
@login_required
def delete(kind, i):
    tb = {"post": "posts", "comment": "comments", "cpost": "cposts"}.get(kind) or abort(404)
    d = db()
    r = d.execute(f"SELECT user_id FROM {tb} WHERE id=?", (i,)).fetchone() or abort(404)
    if r["user_id"] != uid():
        c = d.execute("SELECT c.* FROM communities c JOIN cposts p ON p.cid=c.id WHERE p.id=?", (i,)).fetchone() if kind == "cpost" else None
        if not (c and can_edit_group(c)):
            abort(403)
        notify(r["user_id"], f"Un modérateur a supprimé ton message dans {c['name']}", f"/communaute/{c['id']}")
    if kind == "cpost":
        d.execute("UPDATE communities SET pin=0 WHERE pin=?", (i,))
    d.execute(f"DELETE FROM {tb} WHERE id=?", (i,))
    return redirect(request.referrer or "/")


# ───────────────────────────── Catalogue & fiche (API) ─────────────────────────────
@app.route("/catalogue")
def catalog():
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    a = request.args
    t = a.get("t", "saison")
    day = a.get("j", days[datetime.now(timezone.utc).weekday()])
    g = a.get("g", "")
    so = a.get("so", "POPULARITY_DESC")
    return render_template("catalog.html", title="Catalogue", t=t if t in ("saison", "top", "planning", "manga", "explore") else "saison",
                           day=day if day in days else "monday", g=g if g in GENRES else "", y=str(toint(a.get("y"), 0, 0, 2100) or ""),
                           so=so if so in SORTS else "POPULARITY_DESC", mn=str(toint(a.get("mn"), 0, 0, 9) or ""), GENRES=GENRES, SORTS=SORTS)


@app.route("/serie/<int:mid>")
def media(mid):
    k = "manga" if request.args.get("k") == "manga" else "anime"
    cur = me() and db().execute("SELECT * FROM favs WHERE user_id=? AND mal_id=?", (uid(), mid)).fetchone()
    posts = db().execute(POSTQ + "WHERE p.mal_id=? AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?) ORDER BY p.id DESC LIMIT 10", (uid(), mid, uid())).fetchall()
    return render_template("media.html", title="Fiche", mid=mid, k=k, cur=cur, posts=posts)


@app.route("/liste")
@login_required
def mylist():
    t = request.args.get("t", "cours")
    t = t if t in STATUSES else "cours"
    d = db()
    rows = d.execute("SELECT * FROM favs WHERE user_id=? AND status=? ORDER BY updated DESC,mal_id DESC", (uid(), t)).fetchall()
    counts = {r["status"]: r["n"] for r in d.execute("SELECT status,COUNT(*) n FROM favs WHERE user_id=? GROUP BY status", (uid(),))}
    ids = [r["mal_id"] for r in rows if r["kind"] == "anime" and t in ("cours", "voir")]
    return render_template("mylist.html", title="Ma liste", t=t, rows=rows, counts=counts, ids=ids)


@app.route("/liste/<int:mid>", methods=["POST"])
@login_required
def list_set(mid):
    f, d = request.form, db()
    a = f.get("action", "set")
    cur = d.execute("SELECT * FROM favs WHERE user_id=? AND mal_id=?", (uid(), mid)).fetchone()
    if a == "remove":
        d.execute("DELETE FROM favs WHERE user_id=? AND mal_id=?", (uid(), mid))
        flash("Retiré de ta liste.", "ok")
        return redirect(request.referrer or "/liste")
    st = f.get("status") or (cur["status"] if cur else "voir")
    st = st if st in STATUSES else "voir"
    ep = cur["ep"] if cur else 0
    total = toint(f.get("total"), cur["total"] if cur else 0)
    if a == "inc":
        ep += 1
    elif a == "dec":
        ep = max(0, ep - 1)
    elif f.get("ep") not in (None, ""):
        ep = toint(f.get("ep"), ep)
    if total and ep > total:
        ep = total
    if a in ("inc", "set") and total and ep >= total:
        st = "fini"
    elif a in ("inc", "set") and ep > 0 and st == "voir":
        st = "cours"
    score = toint(f.get("score"), cur["score"] if cur else 0, 0, 10)
    title = txt(f.get("title"), 120) or (cur["title"] if cur else "?")
    img = f.get("img") or (cur["img"] if cur else "")
    img = img if img.startswith("https://") else ""
    kind = "manga" if (f.get("kind") == "manga" or (cur and cur["kind"] == "manga")) else "anime"
    d.execute("INSERT OR REPLACE INTO favs(user_id,mal_id,title,img,status,ep,total,score,kind,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (uid(), mid, title, img, st, ep, total, score, kind, now()))
    if a == "set":
        flash("Ta liste est à jour.", "ok")
    return redirect(request.referrer or "/liste")


# ───────────────────────────── Communautés ─────────────────────────────
GIMG = {"avatar": (70_000, "av"), "cover": (170_000, "cv")}


def can_edit_group(c):
    return bool(me()) and (c["creator_id"] == uid() or is_admin())


def gav(c, size=48):
    """Photo (ou initiale colorée) d'un groupe."""
    r = max(10, size // 4)
    if c["av"]:
        return Markup(f'<img class="gav" src="/g/{c["id"]}/avatar?v={c["av"]}" alt="" loading="lazy" style="width:{size}px;height:{size}px;border-radius:{r}px">')
    h = sum(map(ord, c["name"])) % 360
    return Markup(f'<span class="gav" style="width:{size}px;height:{size}px;border-radius:{r}px;font-size:{size // 2.2}px;'
                  f'background:linear-gradient(135deg,hsl({h} 85% 55%),hsl({(h + 55) % 360} 85% 45%))">{Markup.escape(c["name"][:1].upper())}</span>')


@app.route("/g/<int:cid>/<kind>")
def group_img(cid, kind):
    if kind not in GIMG:
        abort(404)
    r = db().execute("SELECT data FROM cimg WHERE cid=? AND kind=?", (cid, kind)).fetchone() or abort(404)
    resp = app.response_class(bytes(r["data"]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=300"
    return resp


@app.route("/api/groupe/<int:cid>/img/<kind>", methods=["POST"])
def api_gimg(cid, kind):
    if not me():
        return jsonify(error="Connecte-toi d'abord."), 401
    if kind not in GIMG:
        abort(404)
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    if not can_edit_group(c):
        return jsonify(error="Seul le créateur du groupe peut changer ses images."), 403
    data = request.get_data(cache=False)
    if data[:3] != b"\xff\xd8\xff" or not 500 < len(data) <= GIMG[kind][0]:
        return jsonify(error="Image invalide ou trop lourde."), 400
    if not throttle(f"gimg{uid()}", 12, 3600):
        return jsonify(error="Trop d'envois, réessaie plus tard."), 429
    v = int(time.time() - 1.7e9)
    d.execute("DELETE FROM cimg WHERE cid=? AND kind=?", (cid, kind))
    d.execute("INSERT INTO cimg(cid,kind,data) VALUES(?,?,?)", (cid, kind, data))
    d.execute(f"UPDATE communities SET {GIMG[kind][1]}=? WHERE id=?", (v, cid))   # colonne issue du dictionnaire GIMG, jamais de l'utilisateur
    return jsonify(ok=True, v=v)


@app.route("/groupe/<int:cid>/photo/supprimer", methods=["POST"])
@login_required
def del_gimg(cid):
    kind = request.form.get("kind")
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    if kind not in GIMG or not can_edit_group(c):
        abort(403)
    d.execute("DELETE FROM cimg WHERE cid=? AND kind=?", (cid, kind))
    d.execute(f"UPDATE communities SET {GIMG[kind][1]}=0 WHERE id=?", (cid,))
    return redirect(f"/communaute/{cid}")


@app.route("/communautes", methods=["GET", "POST"])
def communities():
    d = db()
    if request.method == "POST":
        if not me():
            return redirect("/connexion")
        n, tp, ds = txt(request.form.get("name"), 40), txt(request.form.get("topic"), 40), txt(request.form.get("descr"), 300)
        if len(n) < 3:
            flash("Nom trop court (3 min).", "err")
        elif d.execute("SELECT 1 FROM communities WHERE name=?", (n,)).fetchone():
            flash("Ce nom existe déjà.", "err")
        else:
            cid = d.execute("INSERT INTO communities(name,topic,descr,creator_id,created) VALUES(?,?,?,?,?)", (n, tp, ds, uid(), now())).lastrowid
            d.execute("INSERT INTO members VALUES(?,?)", (cid, uid()))
            d.execute("INSERT INTO gseen(cid,user_id,seen) VALUES(?,?,0) ON CONFLICT DO NOTHING", (cid, uid()))
            return redirect(f"/communaute/{cid}")
    rows = d.execute("SELECT c.*,(SELECT COUNT(*) FROM members WHERE cid=c.id) n,(SELECT COUNT(*) FROM members WHERE cid=c.id AND user_id=?) joined,"
                     "(SELECT MAX(created) FROM cposts WHERE cid=c.id) la "
                     "FROM communities c ORDER BY n DESC,c.id DESC", (uid(),)).fetchall()
    return render_template("communities.html", title="Communautés", rows=rows, gun=group_unread(d))


REACTS = ["👍", "❤️", "😂", "😮", "😢", "🔥"]
# Taille maximale (octets) par type d'envoi. Les médias sont stockés dans la base : garde-les raisonnables.
MEDIA_MAX = {"image": 600_000, "gif": 1_500_000, "video": 8_000_000, "audio": 3_000_000}
CMAX = 60
# Conservation des médias (photos, vidéos, vocaux) : au-delà, le fichier est supprimé mais le texte du message reste.
# Réglable sans toucher au code : variables d'environnement MEDIA_DAYS et MEDIA_CAP_MB dans Render.
MEDIA_DAYS = int(os.environ.get("MEDIA_DAYS", "25") or 25)
MEDIA_CAP_MB = int(os.environ.get("MEDIA_CAP_MB", "300") or 300)
PURGE = {"t": 0.0}


def media_stats(d):
    one = lambda q: d.execute(q).fetchone()[0] or 0
    files = one("SELECT COUNT(*) FROM cmedia") + one("SELECT COUNT(*) FROM pmedia")
    size = one("SELECT COALESCE(SUM(LENGTH(data)),0) FROM cmedia") + one("SELECT COALESCE(SUM(LENGTH(data)),0) FROM pmedia")
    return files, size


def purge_media(d):
    """Supprime les médias de plus de MEDIA_DAYS jours, puis les plus anciens si le total dépasse MEDIA_CAP_MB.
    Les messages restent : ils affichent « média expiré » à la place du fichier. Retourne le nombre de fichiers supprimés."""
    cut = (datetime.now(timezone.utc) - timedelta(days=MEDIA_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for tb, parent, mark in (("cmedia", "cposts SET mk='expire'", "mk"), ("pmedia", "posts SET has_img=2", "has_img")):
        k = d.execute(f"SELECT COUNT(*) FROM {tb} WHERE created<?", (cut,)).fetchone()[0]
        if k:
            d.execute(f"UPDATE {parent} WHERE id IN (SELECT pid FROM {tb} WHERE created<?)", (cut,))
            d.execute(f"DELETE FROM {tb} WHERE created<?", (cut,))
            n += k
    cap = MEDIA_CAP_MB * 1_000_000
    for _ in range(500):
        if media_stats(d)[1] <= cap:
            break
        old = d.execute("SELECT t,id,pid FROM (SELECT 'c' t,id,pid,created FROM cmedia UNION ALL SELECT 'p' t,id,pid,created FROM pmedia) x ORDER BY created,id LIMIT 10").fetchall()
        if not old:
            break
        for x in old:
            if x["t"] == "c":
                d.execute("UPDATE cposts SET mk='expire' WHERE id=?", (x["pid"],))
                d.execute("DELETE FROM cmedia WHERE id=?", (x["id"],))
            else:
                d.execute("UPDATE posts SET has_img=2 WHERE id=?", (x["pid"],))
                d.execute("DELETE FROM pmedia WHERE id=?", (x["id"],))
            n += 1
            if media_stats(d)[1] <= cap:
                break
    return n


@app.before_request
def auto_purge():
    """Nettoyage automatique, au plus une fois par heure (déclenché par une visite : pas de tâche planifiée à configurer)."""
    if request.method != "GET" or request.path.startswith("/api/") or not READY["ok"] or time.time() - PURGE["t"] < 3600:
        return
    PURGE["t"] = time.time()
    try:
        n = purge_media(db())
        if n:
            log.info("Nettoyage des médias : %s fichier(s) supprimé(s)", n)
    except Exception:
        log.exception("nettoyage des médias")



def sniff(k, b):
    """Reconnaît le vrai format d'après les premiers octets -> (mime, taille max) ou (None, 0)."""
    if len(b) < 200:
        return None, 0
    mp4, webm = b[4:8] == b"ftyp", b[:4] == b"\x1a\x45\xdf\xa3"
    if k == "image":
        if b[:3] == b"\xff\xd8\xff":
            return "image/jpeg", MEDIA_MAX["image"]
        if b[:4] == b"GIF8":
            return "image/gif", MEDIA_MAX["gif"]
    elif k == "video":
        if mp4:
            return ("video/quicktime" if b[8:12] == b"qt  " else "video/mp4"), MEDIA_MAX["video"]
        if webm:
            return "video/webm", MEDIA_MAX["video"]
    elif k == "audio":
        if mp4:
            return "audio/mp4", MEDIA_MAX["audio"]
        if webm:
            return "audio/webm", MEDIA_MAX["audio"]
        if b[:4] == b"OggS":
            return "audio/ogg", MEDIA_MAX["audio"]
    return None, 0


MEN_RE = re.compile(r"(?<![\w@])@([\w.\-]{3,20})")


def snip(body, mk="", title=""):
    """Aperçu court d'un message (pour les citations, l'épingle et la barre de réponse)."""
    if title:
        return f"🎬 {title}"
    if body:
        return body[:90] + ("…" if len(body) > 90 else "")
    return {"image": "📷 Photo", "video": "🎞️ Vidéo", "audio": "🎤 Message vocal", "expire": "📎 Média expiré"}.get(mk or "") or (f"🎬 {title}" if title else "…")


@app.template_filter("ment")
def ment_filter(text):
    """Texte échappé, avec les @pseudo cliquables."""
    from markupsafe import escape as _esc

    def sub(m):
        full = m.group(1)
        name = full.rstrip(".-")
        return m.group(0) if len(name) < 3 else f'<a class="men" href="/profil/{name}">@{name}</a>{full[len(name):]}'
    return Markup(MEN_RE.sub(sub, str(_esc(text or ""))))


def mentions(d, body, cid, skip=()):
    """Membres du groupe cités par @pseudo dans le message -> [(id, pseudo)] (hors moi)."""
    out = {}
    for raw in MEN_RE.findall(body or ""):
        for cand in (raw, raw.rstrip(".-")):
            if len(cand) < 3:
                continue
            u = d.execute("SELECT u.id,u.username FROM users u JOIN members m ON m.user_id=u.id AND m.cid=? WHERE lower(u.username)=lower(?)", (cid, cand)).fetchone()
            if u:
                out[u["id"]] = u["username"]
                break
    return [(i, n) for i, n in out.items() if i != uid() and i not in skip][:8]


def tell(d, to, text, link):
    """Notification, sauf si le destinataire m'a bloqué."""
    if not d.execute("SELECT 1 FROM blocks WHERE user_id=? AND blocked_id=?", (to, uid())).fetchone():
        notify(to, text, link)


def new_cpost(d, c, body, mk="", rid=0, serie=None):
    """Enregistre un message de groupe (texte, média ou série) et prévient les personnes citées ou auxquelles on répond."""
    pid = d.execute("INSERT INTO cposts(cid,user_id,body,created,mk,rid,mal_id,m_title,m_img) VALUES(?,?,?,?,?,?,?,?,?)",
                    (c["id"], uid(), body, now(), mk, rid, serie[0] if serie else 0, serie[1] if serie else "", serie[2] if serie else "")).lastrowid
    link, told, who = f"/communaute/{c['id']}#m{pid}", set(), me()["username"]
    if rid:
        q = d.execute("SELECT user_id FROM cposts WHERE id=?", (rid,)).fetchone()
        if q and q["user_id"] != uid():
            told.add(q["user_id"])
            tell(d, q["user_id"], f"{who} a répondu à ton message dans {c['name']}", link)
    for i, _n in mentions(d, body, c["id"], told):
        tell(d, i, f"{who} t'a mentionné dans {c['name']}", link)
    return pid


def mark_seen(d, cid, upto):
    d.execute("INSERT INTO gseen(cid,user_id,seen) VALUES(?,?,0) ON CONFLICT DO NOTHING", (cid, uid()))
    d.execute("UPDATE gseen SET seen=? WHERE cid=? AND user_id=? AND seen<?", (upto, cid, uid(), upto))
    g.pop("gun", None)


def group_unread(d):
    """{id du groupe: (messages non lus, en sourdine)} pour mes groupes (calculé une seule fois par requête)."""
    if not me():
        return {}
    if "gun" not in g:
        rows = d.execute("SELECT s.cid,s.mute,(SELECT COUNT(*) FROM cposts p WHERE p.cid=s.cid AND p.user_id<>s.user_id AND p.id>s.seen) n "
                         "FROM gseen s JOIN members m ON m.cid=s.cid AND m.user_id=s.user_id WHERE s.user_id=?", (uid(),)).fetchall()
        g.gun = {r["cid"]: (r["n"], r["mute"]) for r in rows}
    return g.gun


def cfeed(d, cid, after=0, before=0, limit=CMAX):
    """Messages d'un groupe en ordre chronologique + réactions : (rows, plus_anciens, compteurs, ma_réaction)."""
    sel = ("SELECT p.id,p.cid,p.user_id,p.body,p.created,p.mk,p.rid,p.mal_id,p.m_title,p.m_img,u.username,"
           "q.body qbody,q.mk qmk,q.m_title qtitle,qu.username qname "
           "FROM cposts p JOIN users u ON u.id=p.user_id LEFT JOIN cposts q ON q.id=p.rid AND p.rid>0 LEFT JOIN users qu ON qu.id=q.user_id "
           "WHERE p.cid=? AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?)")
    more = False
    if after:
        rows = d.execute(sel + " AND p.id>? ORDER BY p.id LIMIT ?", (cid, uid(), after, limit)).fetchall()
    else:
        rows = d.execute(sel + (" AND p.id<?" if before else "") + " ORDER BY p.id DESC LIMIT ?",
                         (cid, uid()) + ((before,) if before else ()) + (limit + 1,)).fetchall()
        more, rows = len(rows) > limit, rows[:limit][::-1]
    rc, rm = {}, {}
    ids = [r["id"] for r in rows]
    if ids:
        ph = ",".join("?" * len(ids))
        for r in d.execute(f"SELECT pid,emo,COUNT(*) n FROM creacts WHERE pid IN ({ph}) GROUP BY pid,emo ORDER BY n DESC,emo", tuple(ids)):
            rc.setdefault(r["pid"], []).append((r["emo"], r["n"]))
        if me():
            for r in d.execute(f"SELECT pid,emo FROM creacts WHERE user_id=? AND pid IN ({ph})", (uid(),) + tuple(ids)):
                rm[r["pid"]] = r["emo"]
    return rows, more, rc, rm


@app.route("/communaute/<int:cid>", methods=["GET", "POST"])
def community(cid):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    joined = bool(me() and d.execute("SELECT 1 FROM members WHERE cid=? AND user_id=?", (cid, uid())).fetchone())
    if request.method == "POST":
        if not me():
            return redirect("/connexion")
        a = request.form.get("action")
        if a == "join":
            if joined:
                d.execute("DELETE FROM members WHERE cid=? AND user_id=?", (cid, uid()))
                d.execute("DELETE FROM gseen WHERE cid=? AND user_id=?", (cid, uid()))
            elif d.execute("SELECT 1 FROM gkick WHERE cid=? AND user_id=?", (cid, uid())).fetchone():
                flash("Tu as été retiré de ce groupe par son créateur.", "err")
            else:
                d.execute("INSERT INTO members VALUES(?,?)", (cid, uid()))
                d.execute("INSERT INTO gseen(cid,user_id,seen) VALUES(?,?,(SELECT COALESCE(MAX(id),0) FROM cposts WHERE cid=?)) ON CONFLICT DO NOTHING", (cid, uid(), cid))
        elif a == "edit" and can_edit_group(c):
            d.execute("UPDATE communities SET topic=?,descr=? WHERE id=?", (txt(request.form.get("topic"), 40), txt(request.form.get("descr"), 300), cid))
            flash("Groupe mis à jour ✨", "ok")
        elif a == "post" and joined and throttle(f"cp{uid()}", 30, 300):
            b = txt(request.form.get("body"), 800)
            if b:
                new_cpost(d, c, b)
        return redirect(f"/communaute/{cid}")
    posts, more, rc, rm = cfeed(d, cid)
    mem = d.execute("SELECT u.username FROM members m JOIN users u ON u.id=m.user_id WHERE cid=? LIMIT 12", (cid,)).fetchall()
    nmem = d.execute("SELECT COUNT(*) FROM members WHERE cid=?", (cid,)).fetchone()[0]
    pin = d.execute("SELECT p.id,p.body,p.mk,p.m_title,u.username FROM cposts p JOIN users u ON u.id=p.user_id WHERE p.id=? AND p.cid=?", (c["pin"], cid)).fetchone() if c["pin"] else None
    names, muted = [], False
    if joined:
        mark_seen(d, cid, d.execute("SELECT COALESCE(MAX(id),0) FROM cposts WHERE cid=?", (cid,)).fetchone()[0])
        names = [r["username"] for r in d.execute("SELECT u.username FROM members m JOIN users u ON u.id=m.user_id WHERE m.cid=? AND u.id<>? ORDER BY u.username LIMIT 300", (cid, uid()))]
        muted = bool((d.execute("SELECT mute FROM gseen WHERE cid=? AND user_id=?", (cid, uid())).fetchone() or {"mute": 0})["mute"])
    return render_template("community.html", title=c["name"], c=c, joined=joined, posts=posts, more=more, rc=rc, rm=rm, mem=mem, nmem=nmem,
                           can_edit=can_edit_group(c), mod=can_edit_group(c), pinid=c["pin"], pin=pin, names=names, muted=muted,
                           last=posts[-1]["id"] if posts else 0, first=posts[0]["id"] if posts else 0)


@app.route("/communaute/<int:cid>/membres")
def group_members(cid):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    rows = d.execute("SELECT u.id,u.username FROM members m JOIN users u ON u.id=m.user_id WHERE m.cid=? ORDER BY u.username", (cid,)).fetchall()
    rows = sorted(rows, key=lambda r: r["id"] != c["creator_id"])
    kicked = d.execute("SELECT u.id,u.username FROM gkick k JOIN users u ON u.id=k.user_id WHERE k.cid=? ORDER BY u.username", (cid,)).fetchall() if can_edit_group(c) else []
    return render_template("gmembers.html", title="Membres", c=c, rows=rows, kicked=kicked, mod=can_edit_group(c))


@app.route("/groupe/<int:cid>/retirer/<int:i>", methods=["POST"])
@login_required
def gkick_user(cid, i):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    if not can_edit_group(c):
        abort(403)
    u = d.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone() or abort(404)
    if u["id"] == c["creator_id"] or u["id"] == uid() or user_is_admin(u):
        flash("Ce membre ne peut pas être retiré.", "err")
    elif d.execute("SELECT 1 FROM members WHERE cid=? AND user_id=?", (cid, i)).fetchone():
        d.execute("DELETE FROM members WHERE cid=? AND user_id=?", (cid, i))
        d.execute("DELETE FROM gseen WHERE cid=? AND user_id=?", (cid, i))
        d.execute("INSERT INTO gkick(cid,user_id,created) VALUES(?,?,?) ON CONFLICT DO NOTHING", (cid, i, now()))
        notify(i, f"Tu as été retiré du groupe {c['name']}", "/communautes")
        flash(f"{u['username']} a été retiré du groupe.", "ok")
    return redirect(f"/communaute/{cid}/membres")


@app.route("/groupe/<int:cid>/readmettre/<int:i>", methods=["POST"])
@login_required
def gkick_undo(cid, i):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    if not can_edit_group(c):
        abort(403)
    d.execute("DELETE FROM gkick WHERE cid=? AND user_id=?", (cid, i))
    flash("Ce membre peut de nouveau rejoindre le groupe.", "ok")
    return redirect(f"/communaute/{cid}/membres")


@app.route("/groupe/<int:cid>/epingler/<int:pid>", methods=["POST"])
@login_required
def gpin(cid, pid):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    if not can_edit_group(c):
        abort(403)
    d.execute("SELECT 1 FROM cposts WHERE id=? AND cid=?", (pid, cid)).fetchone() or abort(404)
    off = c["pin"] == pid
    d.execute("UPDATE communities SET pin=? WHERE id=?", (0 if off else pid, cid))
    flash("Message désépinglé." if off else "Message épinglé 📌", "ok")
    return redirect(f"/communaute/{cid}")


@app.route("/groupe/<int:cid>/sourdine", methods=["POST"])
@login_required
def gmute(cid):
    d = db()
    r = d.execute("SELECT mute FROM gseen WHERE cid=? AND user_id=?", (cid, uid())).fetchone() or abort(404)
    d.execute("UPDATE gseen SET mute=? WHERE cid=? AND user_id=?", (0 if r["mute"] else 1, cid, uid()))
    g.pop("gun", None)
    flash("Alertes réactivées 🔔" if r["mute"] else "Groupe en sourdine 🔕 (tu es toujours prévenu si on te mentionne)", "ok")
    return redirect(f"/communaute/{cid}")


@app.route("/api/groupes/non-lus")
def api_gunread():
    n = sum(x for x, mu in group_unread(db()).values() if not mu)
    resp = jsonify(n=n)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/groupe/<int:cid>/fil")
def api_gfeed(cid):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    big = 10 ** 12
    rows, more, rc, rm = cfeed(d, cid, after=toint(request.args.get("apres"), 0, 0, big), before=toint(request.args.get("avant"), 0, 0, big))
    joined = bool(me() and d.execute("SELECT 1 FROM members WHERE cid=? AND user_id=?", (cid, uid())).fetchone())
    if joined and rows and request.args.get("apres"):
        mark_seen(d, cid, rows[-1]["id"])
    resp = jsonify(html=render_template("cfeed.html", posts=rows, rc=rc, rm=rm, joined=joined, mod=can_edit_group(c), pinid=c["pin"]), n=len(rows), more=more,
                   first=rows[0]["id"] if rows else 0, last=rows[-1]["id"] if rows else 0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/groupe/<int:cid>/envoi", methods=["POST"])
def api_gsend(cid):
    """Envoi d'un message de groupe : texte, photo, GIF, vidéo, message vocal ou série (corps brut de la requête)."""
    if not me():
        return jsonify(error="Connecte-toi d'abord."), 401
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    if not d.execute("SELECT 1 FROM members WHERE cid=? AND user_id=?", (cid, uid())).fetchone():
        return jsonify(error="Rejoins le groupe pour écrire."), 403
    k = request.args.get("k", "texte")
    if k not in ("texte", "image", "video", "audio", "serie"):
        return jsonify(error="Type d'envoi inconnu."), 400
    limit = 4000 if k in ("texte", "serie") else max(MEDIA_MAX["gif"], MEDIA_MAX["image"]) if k == "image" else MEDIA_MAX[k]
    if (request.content_length or 0) > limit:
        return jsonify(error="Fichier trop lourd."), 413
    data = request.get_data(cache=False)
    if len(data) > limit:
        return jsonify(error="Fichier trop lourd."), 413
    rid = toint(request.args.get("r"), 0, 0, 10 ** 12)
    if rid and not d.execute("SELECT 1 FROM cposts WHERE id=? AND cid=?", (rid, cid)).fetchone():
        rid = 0
    if k == "texte":
        body = txt(data.decode("utf-8", "ignore"), 800)
        if not body:
            return jsonify(error="Message vide."), 400
        if not throttle(f"cp{uid()}", 30, 300):
            return jsonify(error="Doucement ! Réessaie dans un instant."), 429
        return jsonify(ok=True, id=new_cpost(d, c, body, "", rid))
    if k == "serie":
        mid, title, img = toint(request.args.get("s"), 0, 0, 10 ** 9), txt(request.args.get("st"), 120), request.args.get("si", "")
        if not mid or not title:
            return jsonify(error="Série invalide."), 400
        if not img.startswith(("https://cdn.myanimelist.net/", "https://s4.anilist.co/")):
            img = ""
        if not throttle(f"cp{uid()}", 30, 300):
            return jsonify(error="Doucement ! Réessaie dans un instant."), 429
        return jsonify(ok=True, id=new_cpost(d, c, txt(request.args.get("t"), 300), "", rid, (mid, title, img)))
    mime, cap = sniff(k, data)
    if not mime:
        return jsonify(error="Format non pris en charge."), 400
    if len(data) > cap:
        return jsonify(error="Fichier trop lourd (max %s Mo)." % round(cap / 1e6, 1)), 413
    if not throttle(f"gm{uid()}", 12, 600) or (k == "video" and not throttle(f"gv{uid()}", 6, 3600)):
        return jsonify(error="Trop d'envois, réessaie un peu plus tard."), 429
    pid = new_cpost(d, c, txt(request.args.get("t"), 300), k, rid)
    d.execute("INSERT INTO cmedia(pid,mime,data,created) VALUES(?,?,?,?)", (pid, mime, data, now()))
    return jsonify(ok=True, id=pid)


def serve_media(table, pid):
    r = db().execute(f"SELECT mime,data FROM {table} WHERE pid=?", (pid,)).fetchone() or abort(404)   # table : valeur interne fixe
    raw = bytes(r["data"])
    resp = send_file(io.BytesIO(raw), mimetype=r["mime"], conditional=True, etag=f"{table}{pid}-{len(raw)}")   # conditional = lecture par morceaux
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/api/groupe/media/<int:pid>")
def gmedia(pid):
    return serve_media("cmedia", pid)


@app.route("/api/post/media/<int:pid>")
def post_media(pid):
    return serve_media("pmedia", pid)


@app.route("/api/groupe/reagir/<int:pid>", methods=["POST"])
def api_react(pid):
    if not me():
        return jsonify(error="Connecte-toi d'abord."), 401
    d = db()
    p = d.execute("SELECT cid FROM cposts WHERE id=?", (pid,)).fetchone() or abort(404)
    if not d.execute("SELECT 1 FROM members WHERE cid=? AND user_id=?", (p["cid"], uid())).fetchone():
        return jsonify(error="Rejoins le groupe pour réagir."), 403
    e = (request.get_json(silent=True) or {}).get("e")
    if e not in REACTS:
        return jsonify(error="Réaction inconnue."), 400
    if not throttle(f"rx{uid()}", 60, 300):
        return jsonify(error="Doucement !"), 429
    cur = d.execute("SELECT emo FROM creacts WHERE pid=? AND user_id=?", (pid, uid())).fetchone()
    d.execute("DELETE FROM creacts WHERE pid=? AND user_id=?", (pid, uid()))
    same = bool(cur and cur["emo"] == e)
    if not same:
        d.execute("INSERT INTO creacts(pid,user_id,emo) VALUES(?,?,?)", (pid, uid(), e))
    counts = [(r["emo"], r["n"]) for r in d.execute("SELECT emo,COUNT(*) n FROM creacts WHERE pid=? GROUP BY emo ORDER BY n DESC,emo", (pid,))]
    return jsonify(ok=True, html=render_template("crx.html", pid=pid, counts=counts, mine="" if same else e))


# ───────────────────────────── Quiz & tournoi ─────────────────────────────
@app.route("/quiz")
def quiz():
    d = db()
    day = now()[:10]
    stats = d.execute("SELECT COUNT(*) n,COALESCE(SUM(good),0) g,COALESCE(SUM(total),0) t,COALESCE(MAX(good),0) b FROM scores WHERE user_id=?", (uid(),)).fetchone() if me() else None
    board = d.execute("SELECT u.username,MAX(s.good) g,MIN(s.ms) ms FROM scores s JOIN users u ON u.id=s.user_id WHERE s.day=? AND s.mode='tournoi' "
                      "GROUP BY u.id ORDER BY g DESC,ms ASC LIMIT 10", (day,)).fetchall()
    played = me() and d.execute("SELECT 1 FROM scores WHERE user_id=? AND day=? AND mode='tournoi'", (uid(), day)).fetchone()
    return render_template("quiz.html", title="Quiz", eta=next_tournament(), stats=stats, board=board, played=played, QC=Q_COUNT, QT=Q_TIME)


@app.route("/api/quiz/finish", methods=["POST"])
@login_required
def api_finish():
    j = request.get_json(silent=True) or {}
    mode = "tournoi" if j.get("mode") == "tournoi" else "solo"
    try:
        total, good, ms = int(j.get("total")), int(j.get("good")), int(j.get("ms"))
    except (TypeError, ValueError):
        return jsonify(error="invalide"), 400
    if not (1 <= total <= Q_COUNT and 0 <= good <= total) or not throttle(f"q{uid()}", 20, 3600):
        return jsonify(error="refusé"), 400
    d = db()
    if mode == "tournoi" and d.execute("SELECT 1 FROM scores WHERE user_id=? AND day=? AND mode='tournoi'", (uid(), now()[:10])).fetchone():
        return jsonify(error="Déjà joué aujourd'hui"), 403
    d.execute("INSERT INTO scores(user_id,mode,good,total,ms,day,created) VALUES(?,?,?,?,?,?,?)", (uid(), mode, good, total, max(ms, total * 1500), now()[:10], now()))
    return jsonify(ok=True)


# ───────────────────────────── Profil, messages, notifications ─────────────────────────────
OTYPES = ["🎭 Cosplayer", "📚 Collectionneur", "📺 Streamer", "✏️ Dessinateur", "📖 Lecteur manga", "🎮 Gamer", "🎵 Mélomane", "🌱 Débutant"]
BADGES = [  # (emoji, nom, description, clé de stat, seuil)
    ("✍️", "Plume", "Publier un post", "np", 1), ("📣", "Voix de la commu", "10 posts", "np", 10),
    ("❤️", "Apprécié", "10 likes reçus", "lk", 10), ("🌟", "Star", "50 likes reçus", "lk", 50),
    ("💬", "Bavard", "10 commentaires", "nc", 10), ("👥", "Sociable", "Rejoindre un groupe", "ng", 1),
    ("🤝", "Pilier de groupes", "Rejoindre 3 groupes", "ng", 3), ("⭐", "Collectionneur", "10 séries en favori", "nf", 10),
    ("🎮", "Quizzeur", "Jouer un quiz", "nq", 1), ("🔥", "Accro au quiz", "10 quiz joués", "nq", 10), ("🧠", "Cerveau", "8/10 au quiz", "best", 8),
]


def user_stats(d, u):
    i = u["id"]
    r = d.execute("SELECT (SELECT COUNT(*) FROM posts WHERE user_id=?) np,(SELECT COUNT(*) FROM likes l JOIN posts p ON p.id=l.post_id WHERE p.user_id=?) lk,"
                  "(SELECT COUNT(*) FROM comments WHERE user_id=?) nc,(SELECT COUNT(*) FROM members WHERE user_id=?) ng,"
                  "(SELECT COUNT(*) FROM favs WHERE user_id=?) nf,(SELECT COUNT(*) FROM scores WHERE user_id=?) nq,(SELECT COALESCE(MAX(good),0) FROM scores WHERE user_id=?) best",
                  (i,) * 7).fetchone()
    stt = dict(r)
    return stt, [dict(e=e, n=n, d=ds, ok=stt[k] >= t) for e, n, ds, k, t in BADGES]


@app.route("/profil/<username>")
def profile(username):
    d = db()
    u = d.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone() or abort(404)
    own = me() and me()["id"] == u["id"]
    if u["privacy"] == "private" and not own:
        return render_template("profile.html", title=username, u=u, locked=True, posts=[], favs=[], st=None)
    posts = d.execute(POSTQ + "WHERE p.user_id=? ORDER BY p.id DESC LIMIT 30", (uid(), u["id"])).fetchall()
    favs = d.execute("SELECT * FROM favs WHERE user_id=? LIMIT 20", (u["id"],)).fetchall()
    st = d.execute("SELECT COUNT(*) n,COALESCE(MAX(good),0) b FROM scores WHERE user_id=?", (u["id"],)).fetchone()
    stt, badges = user_stats(d, u)
    return render_template("profile.html", title=username, u=u, locked=False, posts=posts, favs=favs, st=st, own=own, stt=stt, badges=badges)


@app.route("/u/<username>/<kind>")
def user_img(username, kind):
    if kind not in ("avatar", "banner"):
        abort(404)
    r = db().execute("SELECT i.data FROM images i JOIN users u ON u.id=i.user_id WHERE u.username=? AND i.kind=?", (username, kind)).fetchone() or abort(404)
    resp = app.response_class(bytes(r["data"]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=300"
    return resp


@app.route("/api/img/<kind>", methods=["POST"])
@login_required
def api_img(kind):
    if kind not in ("avatar", "banner"):
        abort(404)
    data = request.get_data(cache=False)
    if data[:3] != b"\xff\xd8\xff" or not 500 < len(data) <= (70000 if kind == "avatar" else 170000):
        return jsonify(error="Image invalide ou trop lourde."), 400
    if not throttle(f"img{uid()}", 12, 3600):
        return jsonify(error="Trop d'envois, réessaie plus tard."), 429
    d, v = db(), int(time.time() - 1.7e9)
    d.execute("DELETE FROM images WHERE user_id=? AND kind=?", (uid(), kind))
    d.execute("INSERT INTO images(user_id,kind,data) VALUES(?,?,?)", (uid(), kind, data))
    if kind == "avatar":
        d.execute("UPDATE users SET av=? WHERE id=?", (v, uid()))
    else:
        d.execute("UPDATE users SET bv=?,p_banner='img' WHERE id=?", (v, uid()))
    PROFS["t"] = 0
    return jsonify(ok=True, v=v)


@app.route("/profil/photo/supprimer", methods=["POST"])
@login_required
def del_img():
    kind = request.form.get("kind")
    if kind not in ("avatar", "banner"):
        abort(404)
    d = db()
    d.execute("DELETE FROM images WHERE user_id=? AND kind=?", (uid(), kind))
    if kind == "avatar":
        d.execute("UPDATE users SET av=0 WHERE id=?", (uid(),))
    else:
        d.execute("UPDATE users SET bv=0,p_banner=CASE WHEN p_banner='img' THEN 'sunset' ELSE p_banner END WHERE id=?", (uid(),))
    PROFS["t"] = 0
    flash("Image supprimée.", "ok")
    return redirect("/profil/modifier")


@app.route("/profil/modifier", methods=["GET", "POST"])
@login_required
def customize():
    u, f = me(), request.form
    if request.method == "POST":
        pick = lambda k, allowed, dflt: f.get(k) if f.get(k) in allowed else dflt
        bn = f.get("banner")
        bn = bn if bn in BANNERS or (bn == "img" and u["bv"]) else "sunset"
        col = f.get("color", "")
        col = col.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", col) else "#ff6a1a"
        db().execute("UPDATE users SET p_ring=?,p_fx=?,p_name=?,p_banner=?,p_color=?,p_status=?,otype=? WHERE id=?",
                     (pick("ring", RINGS, "none"), pick("fx", FXS, "none"), pick("nm", NAMES, "normal"), bn, col, txt(f.get("pstatus"), 60), pick("otype", OTYPES, ""), u["id"]))
        PROFS["t"] = 0
        flash("Profil mis à jour ✨", "ok")
        return redirect("/profil/" + u["username"])
    return render_template("customize.html", title="Personnaliser", back="/parametres", RINGS=RINGS, FXS=FXS, NAMES=NAMES, BANNERS=BANNERS, OTYPES=OTYPES)


@app.route("/messages")
@login_required
def inbox():
    rows = db().execute("""SELECT u.username,m.body,m.created,(SELECT COUNT(*) FROM msgs WHERE s=u.id AND r=? AND read=0) un FROM msgs m
        JOIN users u ON u.id=CASE WHEN m.s=? THEN m.r ELSE m.s END
        WHERE m.id IN (SELECT MAX(id) FROM msgs WHERE s=? OR r=? GROUP BY CASE WHEN s=? THEN r ELSE s END) ORDER BY m.id DESC""",
                        (uid(),) + (uid(),) * 4).fetchall()
    return render_template("inbox.html", title="Messages", rows=rows)


@app.route("/messages/<username>", methods=["GET", "POST"])
@login_required
def thread(username):
    d = db()
    o = d.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone() or abort(404)
    if o["id"] == uid():
        return redirect("/messages")
    if request.method == "POST":
        b = txt(request.form.get("body"), 1000)
        if d.execute("SELECT 1 FROM blocks WHERE (user_id=? AND blocked_id=?) OR (user_id=? AND blocked_id=?)", (uid(), o["id"], o["id"], uid())).fetchone():
            flash("Message impossible : un blocage existe entre vous.", "err")
        elif o["dm"] == "none":
            flash("Cette personne n'accepte pas les messages.", "err")
        elif b and throttle(f"dm{uid()}", 20, 300):
            d.execute("INSERT INTO msgs(s,r,body,created) VALUES(?,?,?,?)", (uid(), o["id"], b, now()))
        return redirect(f"/messages/{username}")
    d.execute("UPDATE msgs SET read=1 WHERE s=? AND r=?", (o["id"], uid()))
    ms = d.execute("SELECT * FROM msgs WHERE (s=? AND r=?) OR (s=? AND r=?) ORDER BY id LIMIT 200", (uid(), o["id"], o["id"], uid())).fetchall()
    return render_template("thread.html", title=username, o=o, ms=ms)


@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    d = db()
    if request.method == "POST":
        d.execute("DELETE FROM notifs WHERE user_id=?", (uid(),)) if request.form.get("a") == "clear" else d.execute("UPDATE notifs SET read=1 WHERE user_id=?", (uid(),))
        return redirect("/notifications")
    rows = d.execute("SELECT * FROM notifs WHERE user_id=? ORDER BY id DESC LIMIT 60", (uid(),)).fetchall()
    return render_template("notifs.html", title="Notifications", rows=rows)


# ───────────────────────────── Paramètres ─────────────────────────────
@app.route("/parametres", methods=["GET", "POST"])
@login_required
def settings():
    d, f, u = db(), request.form, me()
    if request.method == "POST":
        a = f.get("action")
        if a == "prefs":
            em = txt(f.get("email"), 120).lower()
            if em and not EMAIL_RE.fullmatch(em):
                flash("Adresse e-mail invalide.", "err")
                return redirect("/parametres")
            if em and d.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (em, u["id"])).fetchone():
                flash("Cet e-mail est déjà utilisé.", "err")
                return redirect("/parametres")
            d.execute("UPDATE users SET bio=?,email=?,theme=?,notify=?,privacy=?,dm=? WHERE id=?",
                      (txt(f.get("bio"), 300), em, "light" if f.get("theme") == "light" else "dark", int(bool(f.get("notify"))),
                       "private" if f.get("privacy") else "public", "all" if f.get("dm") else "none", u["id"]))
            flash("Paramètres enregistrés.", "ok")
        elif a == "regen":
            if throttle(f"rg{u['id']}", 5, 600) and check_password_hash(u["pw"], f.get("password", "")[:128]):
                code = new_code()
                d.execute("UPDATE users SET recovery=? WHERE id=?", (generate_password_hash(norm_code(code)), u["id"]))
                session["rc"] = code
                return redirect("/bienvenue")
            flash("Mot de passe incorrect.", "err")
        elif a == "password":
            if not throttle(f"pw{u['id']}", 5, 600):
                flash("Trop de tentatives.", "err")
            elif not check_password_hash(u["pw"], f.get("old", "")[:128]):
                flash("Mot de passe actuel incorrect.", "err")
            elif pw_problem(f.get("new", ""), u["username"]):
                flash(pw_problem(f.get("new", ""), u["username"]), "err")
            else:
                sv = u["sv"] + 1
                d.execute("UPDATE users SET pw=?,sv=? WHERE id=?", (generate_password_hash(f["new"]), sv, u["id"]))
                session["sv"] = sv
                flash("Mot de passe changé.", "ok")
        elif a == "logout_all":
            sv = u["sv"] + 1
            d.execute("UPDATE users SET sv=? WHERE id=?", (sv, u["id"]))
            session["sv"] = sv
            flash("Déconnecté des autres appareils.", "ok")
        elif a == "delete":
            if f.get("confirm") == u["username"] and check_password_hash(u["pw"], f.get("password", "")[:128]):
                d.execute("DELETE FROM users WHERE id=?", (u["id"],))
                session.clear()
                return redirect("/")
            flash("Pseudo ou mot de passe incorrect.", "err")
        return redirect("/parametres")
    return render_template("settings.html", title="Paramètres", wa=WA, wa_show=WA_SHOW, email=EMAIL, fb=FB)


@app.route("/export", methods=["POST"])
@login_required
def export():
    d, i = db(), uid()
    q = lambda s: [rowd(r) for r in d.execute(s, (i,)).fetchall()]
    data = dict(profil=dict(username=me()["username"], bio=me()["bio"], created=me()["created"]), posts=q("SELECT body,created FROM posts WHERE user_id=?"),
                favoris=q("SELECT mal_id,title FROM favs WHERE user_id=?"), scores=q("SELECT mode,good,total,day FROM scores WHERE user_id=?"))
    return app.response_class(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                              headers={"Content-Disposition": "attachment; filename=senpaix-export.json"})


# ───────────────────────────── Auth ─────────────────────────────
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def new_code():
    c = secrets.token_hex(6).upper()
    return "-".join(c[i:i + 4] for i in (0, 4, 8))


def norm_code(c):
    return re.sub(r"[^0-9A-Z]", "", (c or "").upper())


def find_user(ident):
    ident = (ident or "").strip()[:120]
    return db().execute("SELECT * FROM users WHERE username=? OR (email!='' AND email=?)", (ident, ident.lower())).fetchone()


@app.route("/api/pseudo")
def api_pseudo():
    n = request.args.get("u", "").strip()
    if not throttle("ps" + str(request.remote_addr), 40, 60):
        return jsonify(ok=False, msg="Trop de vérifications"), 429
    if not re.fullmatch(r"[\w.\-]{3,20}", n) or n.lower() in RESERVED:
        return jsonify(ok=False, msg="Pseudo invalide ou réservé")
    if db().execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (n,)).fetchone():
        return jsonify(ok=False, msg="Pseudo déjà pris")
    return jsonify(ok=True, msg="Pseudo disponible")


@app.route("/inscription", methods=["GET", "POST"])
def register():
    f = request.form
    if request.method == "POST":
        n, em, p = txt(f.get("username"), 20), txt(f.get("email"), 120).lower(), f.get("password", "")
        d = db()
        if not re.fullmatch(r"[\w.\-]{3,20}", n) or n.lower() in RESERVED:
            flash("Pseudo invalide ou réservé (3-20 caractères : lettres, chiffres, . _ -).", "err")
        elif em and not EMAIL_RE.fullmatch(em):
            flash("Adresse e-mail invalide.", "err")
        elif pw_problem(p, n):
            flash(pw_problem(p, n), "err")
        elif p != f.get("password2"):
            flash("Les deux mots de passe sont différents.", "err")
        elif not (f.get("age") and f.get("cgu")):
            flash("Tu dois avoir 13 ans ou plus et accepter les conditions.", "err")
        elif not throttle("reg" + str(request.remote_addr), 5, 3600):
            flash("Trop d'inscriptions, réessaie plus tard.", "err")
        elif d.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (n,)).fetchone():
            flash("Ce pseudo est déjà pris.", "err")
        elif em and d.execute("SELECT 1 FROM users WHERE email=?", (em,)).fetchone():
            flash("Cet e-mail est déjà utilisé.", "err")
        else:
            code = new_code()
            i = d.execute("INSERT INTO users(username,pw,email,recovery,created) VALUES(?,?,?,?,?)",
                          (n, generate_password_hash(p), em, generate_password_hash(norm_code(code)), now())).lastrowid
            session.clear(); session.update(uid=i, sv=0, rc=code); session.permanent = True
            return redirect("/bienvenue")
    return render_template("auth.html", title="Inscription", reg=True, bare=True, v=f)


@app.route("/bienvenue")
@login_required
def welcome():
    code = session.pop("rc", None)
    if not code:
        return redirect("/")
    return render_template("welcome.html", title="Bienvenue", code=code, bare=True)


DUMMY = generate_password_hash("x" + secrets.token_hex(6))


@app.route("/connexion", methods=["GET", "POST"])
def login():
    f = request.form
    if request.method == "POST":
        ident, p = f.get("username", "").strip(), f.get("password", "")[:128]
        if not throttle("log" + str(request.remote_addr) + ident.lower(), 6, 600) or not throttle("acct" + ident.lower(), 20, 3600):
            flash("Trop de tentatives. Attends 10 minutes.", "err")
        else:
            u = find_user(ident)
            if u and u["banned"]:
                flash("Ce compte a été suspendu pour non-respect des conditions." + (f" Motif : {u['ban_reason']}" if u["ban_reason"] else ""), "err")
            elif check_password_hash(u["pw"] if u else DUMMY, p) and u:
                session.clear(); session.update(uid=u["id"], sv=u["sv"]); session.permanent = bool(f.get("remember"))
                nx = request.args.get("next", "/")
                return redirect(nx if nx.startswith("/") and not nx.startswith("//") else "/")
            flash("Pseudo/e-mail ou mot de passe incorrect.", "err")
    return render_template("auth.html", title="Connexion", reg=False, bare=True, v=f)


@app.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def forgot():
    f = request.form
    if request.method == "POST":
        u = find_user(f.get("username"))
        new = f.get("new", "")
        if not throttle("fg" + str(request.remote_addr), 5, 900):
            flash("Trop de tentatives. Réessaie dans 15 minutes.", "err")
        elif pw_problem(new, (u["username"] if u else "")) or new != f.get("new2"):
            flash(pw_problem(new, (u["username"] if u else "")) or "Les deux mots de passe doivent être identiques.", "err")
        elif not u or not u["recovery"] or not check_password_hash(u["recovery"], norm_code(f.get("code"))):
            flash("Pseudo/e-mail ou code de récupération incorrect.", "err")
        else:
            code, sv = new_code(), u["sv"] + 1
            db().execute("UPDATE users SET pw=?,sv=?,recovery=? WHERE id=?", (generate_password_hash(new), sv, generate_password_hash(norm_code(code)), u["id"]))
            session.clear(); session.update(uid=u["id"], sv=sv, rc=code); session.permanent = True
            flash("Mot de passe réinitialisé.", "ok")
            return redirect("/bienvenue")
    return render_template("forgot.html", title="Mot de passe oublié", bare=True, v=f, email=EMAIL)


@app.route("/deconnexion", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


@app.route("/signaler/<kind>/<int:i>", methods=["POST"])
@login_required
def report(kind, i):
    if kind not in ("post", "comment", "cpost", "user"):
        abort(404)
    r = request.form.get("reason")
    r = r if r in REASONS else "Autre"
    d = db()
    if not throttle(f"rp{uid()}", 10, 3600):
        flash("Trop de signalements, réessaie plus tard.", "err")
    elif d.execute("SELECT 1 FROM reports WHERE reporter_id=? AND kind=? AND target_id=? AND status='ouvert'", (uid(), kind, i)).fetchone():
        flash("Tu as déjà signalé ce contenu.", "ok")
    else:
        d.execute("INSERT INTO reports(reporter_id,kind,target_id,reason,created) VALUES(?,?,?,?,?)", (uid(), kind, i, r, now()))
        flash("Merci, ton signalement a été envoyé à la modération.", "ok")
    return redirect(request.referrer or "/")


@app.route("/bloquer/<username>", methods=["POST"])
@login_required
def block(username):
    d = db()
    o = d.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone() or abort(404)
    if o["id"] == uid():
        flash("Tu ne peux pas te bloquer toi-même.", "err")
    elif d.execute("DELETE FROM blocks WHERE user_id=? AND blocked_id=?", (uid(), o["id"])).rowcount:
        flash(f"{username} est débloqué.", "ok")
    else:
        d.execute("INSERT INTO blocks VALUES(?,?)", (uid(), o["id"]))
        flash(f"{username} est bloqué : tu ne verras plus ses publications.", "ok")
    return redirect(request.referrer or "/")


@app.route("/bloques")
@login_required
def blocked():
    rows = db().execute("SELECT u.username FROM blocks b JOIN users u ON u.id=b.blocked_id WHERE b.user_id=?", (uid(),)).fetchall()
    return render_template("blocked.html", title="Utilisateurs bloqués", rows=rows)


def target_info(kind, i):
    d = db()
    if kind == "user":
        r = d.execute("SELECT id uid,username,bio body FROM users WHERE id=?", (i,)).fetchone()
    else:
        tb = {"post": "posts", "comment": "comments", "cpost": "cposts"}[kind]
        r = d.execute(f"SELECT t.user_id uid,u.username,t.body FROM {tb} t JOIN users u ON u.id=t.user_id WHERE t.id=?", (i,)).fetchone()
    if r and kind == "post":
        hi = d.execute("SELECT has_img FROM posts WHERE id=?", (i,)).fetchone()
        if hi and hi["has_img"] == 1:
            r = dict(uid=r["uid"], username=r["username"], body=f"[photo : /api/post/media/{i}] " + (r["body"] or ""))
    if r and kind == "cpost":
        mk = d.execute("SELECT mk FROM cposts WHERE id=?", (i,)).fetchone()
        if mk and mk["mk"]:
            r = dict(uid=r["uid"], username=r["username"], body=f"[{mk['mk']} : /api/groupe/media/{i}] " + (r["body"] or ""))
    return r


def delete_content(kind, i):
    tb = {"post": "posts", "comment": "comments", "cpost": "cposts"}.get(kind)
    if tb:
        db().execute("DELETE FROM %s WHERE id=?" % tb, (i,))
    return bool(tb)


@app.route("/admin")
@admin_only
def admin():
    d = db()
    one = lambda q, a=(): d.execute(q, a).fetchone()[0]
    since = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    stats = dict(users=one("SELECT COUNT(*) FROM users"), u1=one("SELECT COUNT(*) FROM users WHERE created>?", (since(1),)), u7=one("SELECT COUNT(*) FROM users WHERE created>?", (since(7),)),
                 posts=one("SELECT COUNT(*) FROM posts"), comments=one("SELECT COUNT(*) FROM comments"), groups=one("SELECT COUNT(*) FROM communities"),
                 reports=one("SELECT COUNT(*) FROM reports WHERE status='ouvert'"), banned=one("SELECT COUNT(*) FROM users WHERE banned=1"), msgs=one("SELECT COUNT(*) FROM msgs"))
    rows = [dict(r=r, t=target_info(r["kind"], r["target_id"])) for r in
            d.execute("SELECT r.*,u.username rep FROM reports r JOIN users u ON u.id=r.reporter_id WHERE r.status='ouvert' ORDER BY r.id DESC LIMIT 50")]
    last_bk = d.execute("SELECT MAX(created) FROM admin_log WHERE action='sauvegarde'").fetchone()[0]
    meta = dict(storage="PostgreSQL" if PG else "SQLite", durable=DURABLE, since=(d.execute("SELECT MIN(created) FROM users").fetchone()[0] or "")[:10], backup=last_bk,
                old=(not last_bk) or last_bk < (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"))
    log_rows = d.execute("SELECT l.*,u.username adm FROM admin_log l JOIN users u ON u.id=l.admin_id ORDER BY l.id DESC LIMIT 15").fetchall()
    files, size = media_stats(d)
    meta.update(mfiles=files, mmb=round(size / 1e6, 1), dbmb=round(d.execute("SELECT pg_database_size(current_database())").fetchone()[0] / 1e6, 1) if PG else None)
    return render_template("admin.html", title="Modération", tab="reports", stats=stats, rows=rows, log=log_rows, meta=meta)


@app.route("/admin/membres")
@admin_only
def admin_members():
    q = txt(request.args.get("q"), 40)
    rows = db().execute("SELECT u.id,u.username,u.email,u.created,u.banned,u.is_admin,(SELECT COUNT(*) FROM posts WHERE user_id=u.id) np,"
                        "(SELECT COUNT(*) FROM reports r WHERE r.kind='user' AND r.target_id=u.id) nr FROM users u "
                        "WHERE u.username LIKE ? OR u.email LIKE ? ORDER BY u.id DESC LIMIT 100", (f"%{q}%", f"%{q}%")).fetchall()
    return render_template("admin_members.html", title="Membres", tab="members", rows=rows, q=q)


@app.route("/admin/membre/<int:i>")
@admin_only
def admin_user(i):
    d = db()
    u = d.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone() or abort(404)
    last = lambda tb, extra="": d.execute(f"SELECT * FROM {tb} WHERE user_id=? ORDER BY id DESC LIMIT 15", (i,)).fetchall()
    reps = d.execute("SELECT COUNT(*) FROM reports WHERE kind='user' AND target_id=?", (i,)).fetchone()[0]
    return render_template("admin_user.html", title=u["username"], tab="members", u=u, adm=user_is_admin(u), posts=last("posts"), comments=last("comments"), cposts=last("cposts"), reps=reps)


@app.route("/admin/contenus")
@admin_only
def admin_content():
    d = db()
    get = lambda tb, x="": d.execute(f"SELECT t.id,t.body,t.created{x},u.id uid,u.username FROM {tb} t JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 30").fetchall()
    return render_template("admin_content.html", title="Contenus", tab="content", posts=get("posts", ",t.has_img"), comments=get("comments"), cposts=get("cposts", ",t.mk"))


@app.route("/admin/bandes-annonces")
@admin_only
def admin_trailers():
    rows = db().execute("SELECT * FROM trailers ORDER BY pos DESC,id DESC").fetchall()
    return render_template("admin_trailers.html", title="Bandes-annonces", tab="trailers", rows=rows)


@app.route("/admin/action", methods=["POST"])
@admin_only
def admin_action():
    f, d = request.form, db()
    act, i = f.get("act"), toint(f.get("id"))
    if act in ("dismiss", "rdelete", "rban"):
        r = d.execute("SELECT * FROM reports WHERE id=?", (i,)).fetchone()
        if r:
            t = target_info(r["kind"], r["target_id"])
            if act == "rdelete" and t and r["kind"] != "user":
                delete_content(r["kind"], r["target_id"])
                alog("supprime", f"{r['kind']} #{r['target_id']}", f"auteur {t['username']} · signalement : {r['reason']}")
            elif act == "rban" and t:
                ban_user(t["uid"], r["reason"])
            elif act == "dismiss":
                alog("ignore signalement", f"#{r['id']}")
            d.execute("UPDATE reports SET status=? WHERE id=?", ("rejeté" if act == "dismiss" else "traité", r["id"]))
            flash("Signalement traité.", "ok")
    elif act == "ban":
        ban_user(i, txt(f.get("reason"), 120))
    elif act == "unban":
        u = d.execute("SELECT username FROM users WHERE id=?", (i,)).fetchone()
        if u:
            d.execute("UPDATE users SET banned=0,ban_reason='' WHERE id=?", (i,))
            alog("rétablit", u["username"])
            flash("Compte rétabli.", "ok")
    elif act == "delete_user":
        u = d.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone()
        if u and u["id"] != uid() and not user_is_admin(u) and f.get("confirm") == u["username"]:
            d.execute("DELETE FROM users WHERE id=?", (i,))
            alog("supprime le compte", u["username"])
            flash("Compte supprimé définitivement.", "ok")
            return redirect("/admin/membres")
        flash("Suppression refusée (confirmation du pseudo incorrecte ou compte protégé).", "err")
    elif act == "rm_images":
        u = d.execute("SELECT username FROM users WHERE id=?", (i,)).fetchone()
        if u:
            d.execute("DELETE FROM images WHERE user_id=?", (i,))
            d.execute("UPDATE users SET av=0,bv=0,p_banner='sunset',p_status='' WHERE id=?", (i,))
            PROFS["t"] = 0
            alog("retire photo/bannière", u["username"])
            flash("Photo, bannière et statut retirés.", "ok")
    elif act == "purge":
        n = purge_media(d)
        alog("nettoyage médias", f"{n} fichier(s)")
        flash(f"{n} média(s) supprimé(s)." if n else "Rien à nettoyer : tout est récent et sous le plafond.", "ok")
    elif act == "dl_del":
        r = d.execute("SELECT title FROM downloads WHERE id=?", (i,)).fetchone()
        if r:
            d.execute("DELETE FROM downloads WHERE id=?", (i,))
            alog("supprime un téléchargement", r["title"])
            flash("Lien supprimé.", "ok")
    elif act == "dl_toggle":
        r = d.execute("SELECT title,hidden FROM downloads WHERE id=?", (i,)).fetchone()
        if r:
            d.execute("UPDATE downloads SET hidden=? WHERE id=?", (0 if r["hidden"] else 1, i))
            alog("masque un téléchargement" if not r["hidden"] else "réaffiche un téléchargement", r["title"])
    elif act in ("dl_up", "dl_down"):
        ids = [r["id"] for r in d.execute("SELECT id FROM downloads ORDER BY pos DESC,id DESC")]
        if i in ids:
            k = ids.index(i)
            j = k - 1 if act == "dl_up" else k + 1
            if 0 <= j < len(ids):
                ids[k], ids[j] = ids[j], ids[k]
                for n, rid in enumerate(ids):
                    d.execute("UPDATE downloads SET pos=? WHERE id=?", (len(ids) - n, rid))
    elif act == "evt_del":
        r = d.execute("SELECT title FROM events WHERE id=?", (i,)).fetchone()
        if r:
            d.execute("DELETE FROM events WHERE id=?", (i,))
            alog("supprime de la une", r["title"])
            flash("Supprimé.", "ok")
    elif act == "evt_toggle":
        r = d.execute("SELECT title,hidden FROM events WHERE id=?", (i,)).fetchone()
        if r:
            d.execute("UPDATE events SET hidden=? WHERE id=?", (0 if r["hidden"] else 1, i))
            alog("masque à la une" if not r["hidden"] else "réaffiche à la une", r["title"])
    elif act in ("evt_up", "evt_down"):
        ids = [r["id"] for r in d.execute("SELECT id FROM events ORDER BY pos DESC,id DESC")]
        if i in ids:
            k = ids.index(i)
            j = k - 1 if act == "evt_up" else k + 1
            if 0 <= j < len(ids):
                ids[k], ids[j] = ids[j], ids[k]
                for n, rid in enumerate(ids):
                    d.execute("UPDATE events SET pos=? WHERE id=?", (len(ids) - n, rid))
    elif act == "trailer_add":
        y, t, mid = yt_id(f.get("link")), txt(f.get("title"), 100), toint(f.get("mal_id"), 0, 0, 10 ** 9)
        if not y:
            flash("Lien YouTube invalide : colle l'adresse de la vidéo (youtube.com/watch… ou youtu.be/…).", "err")
        elif not t:
            flash("Donne un titre à la bande-annonce.", "err")
        elif d.execute("SELECT COUNT(*) FROM trailers").fetchone()[0] >= 60:
            flash("Maximum 60 bandes-annonces : supprime-en une d'abord.", "err")
        elif d.execute("SELECT 1 FROM trailers WHERE yt=?", (y,)).fetchone():
            flash("Cette vidéo est déjà dans la liste.", "err")
        else:
            top = d.execute("SELECT COALESCE(MAX(pos),0) FROM trailers").fetchone()[0]
            d.execute("INSERT INTO trailers(title,yt,mal_id,stitle,pos,created) VALUES(?,?,?,?,?,?)", (t, y, mid, txt(f.get("m_title"), 120) if mid else "", top + 1, now()))
            alog("ajoute une bande-annonce", t, y)
            flash("Bande-annonce ajoutée 🎬 Elle apparaît en haut de l'accueil.", "ok")
    elif act == "trailer_del":
        r = d.execute("SELECT title FROM trailers WHERE id=?", (i,)).fetchone()
        if r:
            d.execute("DELETE FROM trailers WHERE id=?", (i,))
            alog("supprime une bande-annonce", r["title"])
            flash("Bande-annonce supprimée.", "ok")
    elif act in ("trailer_up", "trailer_down"):
        ids = [r["id"] for r in d.execute("SELECT id FROM trailers ORDER BY pos DESC,id DESC")]
        if i in ids:
            k = ids.index(i)
            j = k - 1 if act == "trailer_up" else k + 1
            if 0 <= j < len(ids):
                ids[k], ids[j] = ids[j], ids[k]
                for n, rid in enumerate(ids):
                    d.execute("UPDATE trailers SET pos=? WHERE id=?", (len(ids) - n, rid))
    elif act == "delete_content":
        kind = f.get("kind")
        if delete_content(kind, i):
            alog("supprime", f"{kind} #{i}")
            flash("Contenu supprimé.", "ok")
    return redirect(request.referrer or "/admin")


def ban_user(i, reason=""):
    d = db()
    u = d.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone()
    if not u or u["id"] == uid() or user_is_admin(u):
        flash("Ce compte ne peut pas être suspendu.", "err")
        return
    d.execute("UPDATE users SET banned=1,ban_reason=?,sv=sv+1 WHERE id=?", (txt(reason, 120), i))
    alog("suspend", u["username"], reason)
    flash(f"{u['username']} est suspendu.", "ok")


@app.route("/admin/export")
@admin_only
def admin_export():
    d, out = db(), {}
    for tb, cols in (("users", "id,username,email,bio,created,banned,is_admin"), ("posts", "*"), ("comments", "*"), ("communities", "*"), ("members", "*"), ("cposts", "*"),
                     ("favs", "*"), ("scores", "*"), ("reports", "*"), ("admin_log", "*")):
        out[tb] = [rowd(r) for r in d.execute(f"SELECT {cols} FROM {tb}").fetchall()]
    alog("sauvegarde", "export JSON")
    return app.response_class(json.dumps(out, ensure_ascii=False, indent=1, default=str), mimetype="application/json",
                              headers={"Content-Disposition": f"attachment; filename=senpaix-sauvegarde-{now()[:10]}.json"})


@app.route("/installation", methods=["GET", "POST"])
def setup():
    if admin_exists(True):
        abort(404)
    f = request.form
    if request.method == "POST":
        n, p = txt(f.get("username"), 20), f.get("password", "")
        d = db()
        u = d.execute("SELECT * FROM users WHERE username=?", (n,)).fetchone()
        if not throttle("setup" + str(request.remote_addr), 8, 600):
            flash("Trop de tentatives, réessaie dans quelques minutes.", "err")
        elif u:
            if check_password_hash(u["pw"], p) and not u["banned"]:
                d.execute("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
                ADM["yes"] = True
                session.clear(); session.update(uid=u["id"], sv=u["sv"]); session.permanent = True
                flash(f"{n} est maintenant administrateur.", "ok")
                return redirect("/admin")
            flash("Mot de passe incorrect pour ce pseudo.", "err")
        elif not re.fullmatch(r"[\w.\-]{3,20}", n) or n.lower() == "modifier":
            flash("Pseudo : 3-20 caractères (lettres, chiffres, . _ -).", "err")
        elif len(p) < 10 or pw_problem(p, n):
            flash(pw_problem(p, n) or "Mot de passe : 10 caractères minimum pour un administrateur.", "err")
        elif p != f.get("password2"):
            flash("Les deux mots de passe sont différents.", "err")
        else:
            i = d.execute("INSERT INTO users(username,pw,is_admin,recovery,created) VALUES(?,?,1,?,?)", (n, generate_password_hash(p), "", now())).lastrowid
            ADM["yes"] = True
            session.clear(); session.update(uid=i, sv=0); session.permanent = True
            flash("Administrateur créé. Bienvenue !", "ok")
            return redirect("/admin")
    return render_template("setup.html", title="Installation", bare=True, v=f)


@app.route("/healthz")
def healthz():
    if not request.args.get("db"):
        return "ok"
    try:
        n = db().execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return f"ok {'postgresql' if PG else 'sqlite'} · {n} membre(s)" + ("" if READY["ok"] else f" · init en attente ({READY['err']})"), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        safe_rollback()
        out = f"ERREUR base : {type(e).__name__}" + (f" [{e.pgcode}]" if getattr(e, "pgcode", None) else "") + (f"\ninit : {READY['err']}" if READY["err"] else "")
        if os.environ.get("SENPAIX_DEBUG") == "1":
            out += "\n" + mask(str(e))[:500]
        return out, 503, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/robots.txt")
def robots():
    return app.response_class("User-agent: *\nDisallow: /admin\nDisallow: /api/\nDisallow: /messages\n", mimetype="text/plain")


_ICONS = {}


def make_png(n):
    import zlib, struct
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        for x in range(n):
            t = (x + y) / (2 * n)
            r, g, b = int(255 - 0 * t), int(106 + 48 * t), int(26 + 34 * t)
            u, v = x / n - .5, y / n - .5
            if abs(u) < .27 and abs(v) < .27 and (abs(u - v) * .7071 < .06 or abs(u + v) * .7071 < .06):
                r, g, b = 255, 255, 255
            raw += bytes((r, g, b))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


@app.route("/icon-<int:n>.png")
def icon_png(n):
    if n not in (192, 512):
        abort(404)
    if n not in _ICONS:
        _ICONS[n] = make_png(n)
    return app.response_class(_ICONS[n], mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/manifest.json")
def manifest():
    m = dict(name="SenpaiX", short_name="SenpaiX", description="L'univers Otaku autrement.", start_url="/", scope="/", display="standalone",
             orientation="portrait", background_color="#070b1a", theme_color="#070b1a", lang="fr",
             icons=[dict(src=f"/icon-{n}.png", sizes=f"{n}x{n}", type="image/png", purpose="any maskable") for n in (192, 512)])
    return app.response_class(json.dumps(m), mimetype="application/manifest+json")


SW = r"""const V='sx-v1';
function trim(){caches.open(V).then(c=>c.keys().then(k=>{if(k.length>300)k.slice(0,k.length-300).forEach(x=>c.delete(x))}))}
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==V).map(x=>caches.delete(x)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);
 if(u.origin===location.origin){if(u.pathname.startsWith('/api/')||u.pathname==='/sw.js')return;
  e.respondWith(fetch(r).then(x=>{if(x.ok){const c=x.clone();caches.open(V).then(h=>h.put(r,c))}return x}).catch(()=>caches.match(r).then(m=>m||caches.match('/')||new Response('Hors connexion',{status:503}))))}
 else if(/myanimelist\.net|anilist\.co/.test(u.hostname)){e.respondWith(caches.match(r).then(m=>m||fetch(r).then(x=>{const c=x.clone();caches.open(V).then(h=>{h.put(r,c);trim()});return x})))}});
"""


@app.route("/sw.js")
def sw():
    return app.response_class(SW, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.route("/diagnostic")
def diagnostic():
    return render_template("diag.html", title="Diagnostic")


@app.route("/confidentialite")
def privacy_page():
    return render_template("priv.html", title="Confidentialité", creator=CREATOR, email=EMAIL)


@app.route("/conditions")
def terms_page():
    return render_template("cgu.html", title="Conditions", creator=CREATOR, email=EMAIL)


@app.route("/a-propos")
def about():
    return render_template("about.html", title="À propos", creator=CREATOR, bio=CREATOR_BIO, wa=WA, wa_show=WA_SHOW, email=EMAIL, fb=FB)


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
       "connect-src 'self' https://graphql.anilist.co https://api.jikan.moe; manifest-src 'self'; worker-src 'self'; frame-src https://www.youtube-nocookie.com; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def secure(r):
    h = r.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h.setdefault("Referrer-Policy", "same-origin")
    h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if request.path != "/sw.js":          # le service worker doit pouvoir charger les affiches externes
        h["Content-Security-Policy"] = CSP
    if PROD:
        h["Strict-Transport-Security"] = "max-age=31536000"
    if r.mimetype == "text/html":
        h["Cache-Control"] = "private, no-cache"
    return r


@app.errorhandler(500)
def err500(e):
    log.exception("Erreur 500")
    safe_rollback()
    kind = type(getattr(e, "original_exception", None) or e).__name__
    return render_template("err.html", title="Erreur", code=500, msg=f"Erreur interne du serveur. Réessaie ; si le problème continue, préviens l'administrateur. ({kind})"), 500


@app.errorhandler(404)
@app.errorhandler(403)
@app.errorhandler(400)
def err(e):
    return render_template("err.html", title="Oups", code=e.code), e.code


@app.errorhandler(DBError)
def err_db(e):
    log.exception("Erreur base de données")
    safe_rollback()
    return render_template("err.html", title="Indisponible", code=503, msg=f"La base de données ne répond pas pour le moment. Réessaie dans quelques secondes. ({type(e).__name__})"), 503, {"Retry-After": "5"}


# ───────────────────────────── Style (thème Otakun : nuit + orange) ─────────────────────────────
CSS = r"""
:root{--bg:#070b1a;--bg2:#0d1428;--card:#121a30;--card2:#18213c;--line:#222c4a;--text:#f3f5ff;--mut:#8f9bbd;--or:#ff6a1a;--or2:#ff9a3c;--red:#e5233b;--r:18px}
html[data-theme=light]{--bg:#f4f5fa;--bg2:#fff;--card:#fff;--card2:#eef0f7;--line:#dde0ee;--text:#14182b;--mut:#626b8a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:radial-gradient(120% 50% at 50% 0,#2a1608 0,transparent 55%) no-repeat,var(--bg);color:var(--text);font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif;padding-bottom:84px}
html[data-theme=light] body{background:var(--bg)}
a{color:inherit;text-decoration:none}.mut{color:var(--mut)}.ic{flex:none;vertical-align:middle}
header{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(14px);padding-top:env(safe-area-inset-top)}
.top{max-width:640px;margin:auto;display:flex;align-items:center;gap:14px;padding:10px 16px}
.top .sp{flex:1}.top .b{position:relative;display:flex}.top .b i{position:absolute;top:-6px;right:-8px;background:var(--red);color:#fff;border-radius:99px;font:700 .62rem/1 sans-serif;padding:3px 5px;font-style:normal}
.menu{position:relative}.menu summary{list-style:none;cursor:pointer;display:flex;padding:2px}.menu summary::-webkit-details-marker{display:none}
.mn{position:absolute;right:0;top:36px;min-width:210px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:6px;box-shadow:0 12px 40px #000a}
.mn a,.mn button{display:flex;align-items:center;gap:12px;width:100%;padding:12px;border-radius:12px;background:none;color:var(--text);font:600 .92rem inherit;font-family:inherit;justify-content:flex-start}.mn a:active,.mn button:active{background:var(--card2)}
.sk{background:linear-gradient(90deg,var(--card) 25%,var(--card2) 50%,var(--card) 75%);background-size:200% 100%;animation:sh 1.2s infinite;border-radius:10px}@keyframes sh{to{background-position:-200% 0}}
.more{position:relative}.more summary{list-style:none;cursor:pointer;color:var(--mut);display:flex;padding:4px}.more summary::-webkit-details-marker{display:none}.mn select{font-size:.85rem;padding:9px}
.avw{display:inline-flex;position:relative;flex:none;border-radius:50%;vertical-align:middle}.avw .ava{position:relative;z-index:1;border-radius:50%;object-fit:cover}
.avw[class*=ring-]:not(.ring-none) .ava{box-shadow:0 0 0 2px var(--bg)}.avw[class*=ring-]:not(.ring-none)::before{content:"";position:absolute;inset:-3px;border-radius:50%;z-index:0}
.ring-flame::before{background:conic-gradient(#ff3d00,#ffb300,#ff6a1a,#ff3d00);animation:spin 2.4s linear infinite}.ring-rainbow::before{background:conic-gradient(red,orange,yellow,lime,cyan,blue,magenta,red);animation:spin 3s linear infinite}
.ring-sakura::before{background:conic-gradient(#ff9ecb,#fff,#ff5fa2,#ff9ecb);animation:spin 3.5s linear infinite}.ring-ice::before{background:conic-gradient(#9be7ff,#fff,#3aa8ff,#9be7ff);animation:spin 3s linear infinite}
.ring-neon::before{background:var(--c);box-shadow:0 0 10px var(--c),0 0 22px var(--c);animation:pulse 1.6s ease-in-out infinite}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes pulse{50%{opacity:.5;transform:scale(1.07)}}@keyframes shift{to{background-position:200% 0}}
@keyframes fall{0%{transform:translateY(-12%) rotate(0);opacity:0}10%{opacity:1}100%{transform:translateY(170px) rotate(360deg);opacity:.2}}
@keyframes rise{0%{transform:translateY(160px) scale(.7);opacity:0}15%{opacity:1}100%{transform:translateY(-20px) scale(1.1);opacity:0}}@keyframes twk{0%,100%{opacity:.15;transform:scale(.6)}50%{opacity:1;transform:scale(1.25)}}
.pcard{margin:0 -16px}.pbanner{position:relative;height:150px;background-size:cover;background-position:center;overflow:hidden}.pmeta{padding:0 16px;text-align:center;margin-top:-46px;position:relative}
.bn-sunset{background:linear-gradient(120deg,#ff6a1a,#ff3d71 60%,#6a1b9a)}.bn-ocean{background:linear-gradient(120deg,#0f3d6e,#1ea0d6 60%,#7be3ff)}.bn-sakura{background:linear-gradient(120deg,#ff9ecb,#ffd6e8 55%,#c58cff)}
.bn-night{background:linear-gradient(120deg,#070b1a,#1b2a5e 60%,#5a2a8a)}.bn-forest{background:linear-gradient(120deg,#0b3d2e,#2e8b57 60%,#b5e48c)}.bn-cyber{background:linear-gradient(120deg,#0ff,#a0f 55%,#f06)}.bn-img{background-color:var(--card)}
.fxbox{position:absolute;inset:0;overflow:hidden;pointer-events:none}.fxp{position:absolute;top:0;animation-timing-function:linear;animation-iteration-count:infinite}.fxp.fall{animation-name:fall}.fxp.rise{animation-name:rise}.fxp.twk{animation-name:twk;animation-timing-function:ease-in-out}
.nm{margin:10px 0 2px;display:inline-block}.nm-flame{background:linear-gradient(90deg,#ff3d00,#ffb300,#ff3d00);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shift 3s linear infinite}
.nm-rainbow{background:linear-gradient(90deg,red,orange,#ee0,lime,cyan,#68f,magenta,red);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shift 4s linear infinite}
.nm-neon{color:#fff;text-shadow:0 0 6px var(--c),0 0 18px var(--c);animation:pulse 2.2s ease-in-out infinite}.pstatus{display:inline-block;background:var(--card2);border-radius:99px;padding:5px 14px;font-size:.82rem;margin:4px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;padding:0 4px}.chp{display:inline-block;margin:0;cursor:pointer}.chp input{display:none}.chp span{display:inline-block;padding:9px 14px;border-radius:99px;background:var(--card2);font-weight:600;font-size:.85rem;color:var(--mut)}.chp input:checked+span{background:linear-gradient(100deg,var(--or),var(--or2));color:#fff}
@media (prefers-reduced-motion:reduce){.avw::before,.nm{animation:none!important}}
.logo{font:900 1.25rem/1 "Trebuchet MS",sans-serif;letter-spacing:-.5px}.logo b{color:var(--or)}
main{max-width:640px;margin:auto;padding:4px 16px}
h1{font-size:1.5rem;margin:6px 0 12px;font-weight:800}h2{font-size:1.02rem;margin:20px 0 10px;font-weight:800}
.hero{font:900 2.1rem/1.02 "Trebuchet MS",sans-serif;text-align:center;margin:12px 0 16px}.hero em{font-style:normal;color:var(--or);display:block;font-size:2.6rem}
.card{background:linear-gradient(160deg,var(--card),var(--bg2));border:1px solid var(--line);border-radius:var(--r);padding:14px;margin:10px 0}
.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;border-radius:99px;padding:11px 20px;background:linear-gradient(100deg,var(--or),var(--or2));color:#fff;font:700 .92rem inherit;font-family:inherit;cursor:pointer}
.btn:active,button:active{transform:scale(.97)}.btn.g{background:transparent;border:1.5px solid var(--line);color:var(--text)}.btn.s{padding:7px 14px;font-size:.8rem}.btn.w{width:100%}.btn.r{background:#7a1c2e}
input,textarea,select{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:var(--bg2);color:var(--text);font:inherit}
textarea{min-height:84px;resize:vertical}label{display:block;margin:12px 0 5px;color:var(--mut);font-size:.8rem;font-weight:600}
.ava{display:inline-flex;align-items:center;justify-content:center;border-radius:50%;color:#fff;font-weight:800;flex:none}
.stories{display:flex;gap:12px;overflow-x:auto;padding:4px 0 8px;scrollbar-width:none}.stories::-webkit-scrollbar{display:none}
.st{flex:none;width:74px;text-align:center;font-size:.68rem}.st .ring{width:72px;height:72px;border-radius:50%;padding:2.5px;background:conic-gradient(var(--or),#ff3d71,var(--or2),var(--or))}
.st img{width:100%;height:100%;border-radius:50%;object-fit:cover;border:2.5px solid var(--bg);display:block;background:var(--card)}
.st span{display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pills{display:flex;gap:8px;overflow-x:auto;margin:6px 0 10px;scrollbar-width:none}
.pill{flex:none;padding:9px 16px;border-radius:99px;background:var(--card2);font-size:.82rem;font-weight:700;display:flex;align-items:center;gap:6px;color:var(--mut)}.pill.on{background:#fff;color:#111}
html[data-theme=light] .pill.on{background:#14182b;color:#fff}
.promo{display:flex;gap:12px;align-items:center;background:linear-gradient(100deg,#a8101f,#4a0a14);border-radius:var(--r);padding:14px;margin:10px 0;color:#fff}
.promo .lg{width:46px;height:46px;border-radius:12px;background:#fff;color:#111;display:flex;align-items:center;justify-content:center;font:900 1.1rem sans-serif}
.promo b{display:block}.promo small{opacity:.85;font-size:.76rem}
.post{padding:12px 0;border-bottom:1px solid var(--line)}.ph{display:flex;gap:10px;align-items:center}.ph b{font-size:.9rem}.ph small{display:block;color:var(--mut);font-size:.72rem}
.post p{margin:8px 0 6px;white-space:pre-wrap;word-break:break-word}
.att{display:flex;gap:10px;background:var(--card2);border-radius:14px;padding:8px;margin:6px 0;align-items:center}.att img{width:44px;height:62px;object-fit:cover;border-radius:8px}
.act{display:flex;gap:18px;color:var(--mut);font-size:.82rem;align-items:center}.act form{display:inline}.act button{background:none;padding:0;color:inherit;font-weight:600;gap:5px}.act .on{color:var(--or)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.tile img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:14px;background:var(--card);display:block}
.tile b{display:block;font-size:.74rem;margin-top:5px;line-height:1.2;max-height:2.4em;overflow:hidden}.tile small{color:var(--or2);font-size:.7rem}
.row{display:flex;align-items:center;gap:12px}.grow{flex:1;min-width:0}
.grp{background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;margin:8px 0}
.it{display:flex;align-items:center;gap:14px;padding:14px 16px;border-bottom:1px solid var(--line);width:100%;background:none;border-radius:0;color:var(--text);font-weight:500;text-align:left}.it:last-child{border:0}
.it .ico{width:34px;height:34px;border-radius:10px;background:var(--card2);color:var(--or);display:flex;align-items:center;justify-content:center;flex:none}
.it small{display:block;color:var(--mut);font-size:.74rem;font-weight:400}.it .grow{font-size:.95rem}.it .ic:last-child{color:var(--mut)}
.sec{color:var(--mut);font-size:.72rem;font-weight:800;letter-spacing:.8px;text-transform:uppercase;margin:20px 6px 6px}
.sw{position:relative;width:48px;height:28px;flex:none;margin:0}.sw input{opacity:0;width:100%;height:100%;position:absolute;margin:0;z-index:2}
.sw i{position:absolute;inset:0;border-radius:99px;background:var(--card2);border:1px solid var(--line);transition:.2s}.sw i:after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.2s}
.sw input:checked+i{background:var(--or);border-color:var(--or)}.sw input:checked+i:after{transform:translateX(20px)}
.tabs{position:fixed;bottom:0;left:0;right:0;z-index:30;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(16px);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.tabs div{max-width:640px;margin:auto;display:flex;justify-content:space-around;padding:6px 4px}.tabs a{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:.66rem;color:var(--mut);padding:6px 10px;border-radius:99px;font-weight:600}
.tabs a.on{color:var(--or)}.tabs a.mid{background:linear-gradient(120deg,var(--or),var(--or2));color:#fff;margin-top:-18px;padding:12px 16px;box-shadow:0 6px 22px #ff6a1a66}
.flash{padding:11px 14px;border-radius:14px;margin:8px 0;font-size:.88rem;background:var(--card2)}.flash.err{background:#4a1522;color:#ffc2cd}.flash.ok{background:#12351f;color:#bff3d0}
.cd{display:flex;gap:8px;justify-content:center;margin:10px 0}.cd div{background:var(--card2);border-radius:12px;padding:8px 12px;text-align:center;min-width:62px}.cd b{font-size:1.35rem;display:block}.cd small{font-size:.62rem;color:var(--mut)}
.kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center}.kpi div{background:var(--card2);border-radius:14px;padding:10px 4px}.kpi b{font-size:1.2rem;color:var(--or2);display:block}.kpi small{color:var(--mut);font-size:.68rem}
.qbar{height:6px;background:var(--card2);border-radius:9px;overflow:hidden;margin:8px 0}.qbar i{display:block;height:100%;background:linear-gradient(90deg,var(--or),var(--or2));width:100%}
.opt{display:block;width:100%;text-align:left;margin:8px 0;padding:14px;border-radius:14px;background:var(--card2);border:1.5px solid var(--line);color:var(--text);font-weight:600}.opt.ok{background:#12351f;border-color:#2ecc71}.opt.no{background:#4a1522;border-color:#e5233b}
.qimg{width:150px;aspect-ratio:2/3;object-fit:cover;border-radius:14px;display:block;margin:8px auto}
.bubble{max-width:80%;padding:9px 13px;border-radius:18px;margin:5px 0;background:var(--card2);word-break:break-word}.bubble.me{margin-left:auto;background:linear-gradient(100deg,var(--or),var(--or2));color:#fff}
.auth{max-width:420px;margin:5vh auto 30px;text-align:center}.auth form{text-align:left}.auth h1{margin-top:0}
.seg{display:flex;background:var(--card2);border-radius:99px;padding:4px;margin-bottom:12px}.seg a{flex:1;padding:10px;border-radius:99px;font-weight:700;color:var(--mut)}.seg a.on{background:linear-gradient(100deg,var(--or),var(--or2));color:#fff}
.chk{display:flex;gap:10px;align-items:flex-start;color:var(--text);font-size:.85rem;font-weight:500;margin:10px 0}.chk input{width:auto;margin-top:3px;accent-color:var(--or)}
.hint{display:block;color:var(--mut);font-size:.74rem;margin-top:4px;min-height:1em}.pw{position:relative}.pw input{padding-right:74px}.eye{position:absolute;right:6px;top:6px;padding:7px 12px;font-size:.75rem;background:var(--card2);color:var(--text)}
.meter{height:5px;background:var(--card2);border-radius:9px;margin-top:8px;overflow:hidden}.meter i{display:block;height:100%;width:0;transition:.25s}
.ghd{display:flex;align-items:center;gap:12px;margin:4px 0 6px}
.cm{display:flex;align-items:flex-end;gap:8px;margin:10px 0}.cm.mine{flex-direction:row-reverse}
.cb{max-width:min(84%,420px);min-width:0;background:var(--card2);border-radius:18px 18px 18px 6px;padding:8px 12px 6px;position:relative}
.cm.mine .cb{background:linear-gradient(100deg,var(--or),var(--or2));color:#fff;border-radius:18px 18px 6px 18px}
.cn{display:block;font-weight:800;font-size:.78rem;color:var(--or2);margin-bottom:2px}
.cb p{margin:2px 0 4px;white-space:pre-wrap;word-break:break-word}
.cimg{display:block;max-width:100%;max-height:340px;min-width:160px;min-height:100px;border-radius:12px;margin:2px 0 6px;background:var(--card);object-fit:contain;cursor:zoom-in}
.cvid{display:block;width:min(66vw,320px);max-height:380px;border-radius:12px;margin:2px 0 6px;background:#000}
.caud{display:block;width:min(64vw,250px);height:40px;margin:2px 0 4px}
.cf{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.cf small{font-size:.68rem;opacity:.75}
.rx{display:flex;gap:4px;flex-wrap:wrap;align-items:center;margin-left:auto}
.rch{padding:2px 8px;border-radius:99px;background:#0003;color:inherit;font:600 .74rem inherit;font-family:inherit;gap:4px}.rch b{font-weight:700}
.rch.on{outline:1.5px solid var(--or2);background:#ff6a1a33}.rch.bump{animation:bump .35s}@keyframes bump{40%{transform:scale(1.35)}}
.rpk{position:relative}.rpk summary{list-style:none;cursor:pointer;display:flex;padding:2px;opacity:.7}.rpk summary::-webkit-details-marker{display:none}
.rrow{position:absolute;bottom:28px;left:-8px;display:flex;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:99px;padding:4px 6px;box-shadow:0 8px 28px #000a;z-index:5}
.cm.mine .rrow{left:auto;right:-8px}.rrow button{background:none;padding:4px 5px;font-size:1.2rem;border-radius:50%;color:inherit}
.cmore summary{opacity:.7;color:inherit}.cmn{top:auto;bottom:26px;right:0;color:var(--text)}
.pop{animation:pop .35s cubic-bezier(.2,1.4,.4,1)}@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.96)}}
.cbar{position:fixed;left:0;right:0;bottom:0;z-index:40;background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(14px);border-top:1px solid var(--line);padding:8px 0 calc(8px + env(safe-area-inset-bottom))}
.cbar>div{max-width:640px;margin:auto;padding:0 12px}.crow{display:flex;align-items:flex-end;gap:4px}
.crow textarea{min-height:42px;max-height:120px;height:42px;resize:none;border-radius:22px;padding:10px 14px;margin:0}
.cbar .ib{background:none;color:var(--mut);padding:9px;border-radius:50%;margin:0;cursor:pointer;display:inline-flex;flex:none}.cbar .ib:active{background:var(--card2)}
.cbar .send{padding:0;width:42px;height:42px;flex:none}.cbar .send:disabled{opacity:.5}
.tray{display:flex;align-items:center;gap:10px;background:var(--card2);border-radius:16px;padding:8px;margin-bottom:8px;position:relative;overflow:hidden}
.tp img,.tp video{width:56px;height:56px;border-radius:10px;object-fit:cover;display:block}.tp audio{height:36px;width:190px}
.pbar{position:absolute;left:0;right:0;bottom:0;height:3px}.pbar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--or),var(--or2));transition:width .15s}
.rec{display:flex;align-items:center;gap:10px;padding:4px 2px}.rec .dot{width:12px;height:12px;border-radius:50%;background:var(--red);animation:pulse 1s infinite}
.cbar [hidden]{display:none!important}
.dli{width:56px;height:56px;border-radius:14px;object-fit:cover;flex:none;background:var(--card2)}.dlf{display:flex;align-items:center;justify-content:center;font-size:1.6rem}
.dlc{margin:10px 0}.dlc p{margin:10px 0 0}.dlc .btn{margin-top:12px}
.evstrip{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;padding:2px 0 12px}
.evc{position:relative;flex:none;width:86%;max-width:420px;aspect-ratio:16/9;border-radius:18px;overflow:hidden;scroll-snap-align:start;background:var(--card2);display:block;color:#fff}
.evc img{width:100%;height:100%;object-fit:cover;display:block}
.evc .evt{position:absolute;left:0;right:0;bottom:0;padding:28px 14px 12px;background:linear-gradient(#0000,#000d)}
.evc .evt b{display:block;font-size:1.02rem;line-height:1.2}.evc .evt small{opacity:.85}
.evk{position:absolute;top:10px;left:10px;background:#000a;backdrop-filter:blur(6px);padding:4px 10px;border-radius:99px;font-size:.72rem;font-weight:700}
.evgrid{display:grid;gap:14px}.evgrid .evc{width:100%;max-width:none}
.evhero{width:100%;border-radius:18px;display:block;background:var(--card2)}
.tstrip{display:flex;gap:12px;overflow-x:auto;padding:2px 0 10px;scroll-snap-type:x proximity}
.tcard{flex:none;width:230px;color:var(--text);scroll-snap-align:start;display:block}
.tcard .tth{position:relative;display:block;border-radius:14px;overflow:hidden;aspect-ratio:16/9;background:#000}
.tcard img{width:100%;height:100%;object-fit:cover;display:block}
.tcard .tth i{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-style:normal;font-size:1.6rem;color:#fff;background:linear-gradient(#0000,#0009)}
.tcard b{margin-top:6px;font-size:.86rem;font-weight:700;line-height:1.25;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}.tgrid .tcard{width:auto}
.sheet.tvm{align-items:center;padding:12px}.tbox{width:100%;max-width:720px;background:var(--card);border-radius:20px;padding:10px;margin:auto;animation:up .28s cubic-bezier(.2,.9,.3,1)}
.tfr{aspect-ratio:16/9;border-radius:14px;overflow:hidden;background:#000}.tfr iframe{width:100%;height:100%;border:0}
.cm.hl .cb{animation:hl 1.7s}@keyframes hl{0%,60%{box-shadow:0 0 0 3px var(--or2)}100%{box-shadow:0 0 0 0 transparent}}
.qt{display:block;border-left:3px solid var(--or2);background:#0003;border-radius:8px;padding:4px 8px;margin:2px 0 6px;font-size:.78rem;cursor:pointer;color:inherit}
.qt b{display:block;color:var(--or2);font-size:.74rem}.qt span{display:block;opacity:.85;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:230px}
.cm.mine .qt{background:#0002;border-left-color:#fff}.cm.mine .qt b{color:#fff}
.men{color:var(--or2);font-weight:700}.cm.mine .men{color:#fff;text-decoration:underline}
.rpl{background:none;color:inherit;padding:2px;opacity:.7;display:inline-flex}
.pinb{display:flex;align-items:center;gap:10px;background:var(--card2);border:1px solid var(--line);border-left:3px solid var(--or);border-radius:14px;padding:8px 12px;margin:8px 0;color:var(--text)}
.pinb div{min-width:0}.pinb b{display:block;font-size:.76rem;color:var(--or2)}.pinb span{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:.85rem}
.sug{display:flex;gap:6px;overflow-x:auto;padding:0 0 8px}.sug button{background:var(--card2);color:var(--text);padding:6px 12px;border-radius:99px;font-size:.82rem;flex:none}
.attp{display:flex;gap:18px;padding:4px 4px 10px}
.ai{display:flex;flex-direction:column;align-items:center;gap:4px;font:600 .72rem/1.2 inherit;font-family:inherit;color:var(--mut);cursor:pointer;background:none;padding:0;border:0;box-shadow:none}
.ai span{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--card2);color:var(--or2)}
.rres{display:flex;align-items:center;gap:12px;width:100%;text-align:left;background:var(--card2);color:var(--text);padding:8px;border-radius:14px;margin:6px 0}
.rres img{width:40px;height:58px;object-fit:cover;border-radius:8px;flex:none}.rres b{font-weight:600}
.tabs a:not(.mid){position:relative}.nb{position:absolute;top:0;left:calc(50% + 4px);min-width:17px;height:17px;border-radius:9px;background:var(--red);color:#fff;font:700 .64rem/17px sans-serif;text-align:center;padding:0 4px;font-style:normal}
.gn{background:var(--red);color:#fff;border-radius:99px;font:700 .72rem/1 sans-serif;padding:4px 8px;flex:none}
.cm.mine .rpl,.cm.mine .cmore summary{color:#fff}
.gone{display:flex;align-items:center;gap:6px;padding:10px 12px;border-radius:12px;background:#0003;color:var(--mut);font-size:.82rem;margin:6px 0}.cm.mine .gone{color:#fff;background:#0002}
.ptools{display:flex;align-items:center;gap:4px;margin-top:8px}
.ptools .ib{background:none;color:var(--mut);padding:8px;border-radius:50%;display:inline-flex;cursor:pointer;margin:0}.ptools .ib.on{color:var(--or2);background:var(--card2)}
.pprev{position:relative;display:inline-block;margin-top:8px}.pprev img{max-height:130px;max-width:100%;border-radius:14px;display:block}
.pprev button{position:absolute;top:6px;right:6px;width:28px;height:28px;padding:0;border-radius:50%;background:#000b;color:#fff}.pprev[hidden],.ppoll[hidden]{display:none}
.ppoll{display:grid;gap:6px;margin-top:8px}.ppoll input{margin:0}
.pimg{display:block;width:100%;max-height:440px;object-fit:cover;border-radius:14px;margin:8px 0;cursor:zoom-in;background:var(--card2)}
.poll{margin:8px 0}.pos{display:grid;gap:8px;position:relative}
.po{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:space-between;gap:10px;text-align:left;background:var(--card2);color:var(--text);border:1px solid var(--line);border-radius:14px;padding:12px 14px;font-weight:600;margin:0}
.po i{position:absolute;left:0;top:0;bottom:0;width:var(--p);background:#ff6a1a33;transition:width .5s;z-index:0}.po span,.po b{position:relative;z-index:1}.po.on{border-color:var(--or2)}.po.on i{background:#ff6a1a66}
.poll small{display:block;margin-top:6px}
.poll.vs .pos{grid-template-columns:1fr 1fr}.poll.vs .po{flex-direction:column;justify-content:center;text-align:center;min-height:92px}
.poll.vs .po i{inset:auto 0 0 0;width:100%;height:var(--p)}
.poll.vs .pos::after{content:"VS";position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:34px;height:34px;border-radius:50%;background:var(--or);color:#fff;font-weight:900;font-size:.72rem;display:grid;place-items:center;z-index:3;box-shadow:0 4px 14px #0008}
.gtools{margin:10px 0 4px}.gtools input{margin:0;border-radius:99px;padding-left:18px}
.gcard{display:flex;align-items:center;gap:12px;padding:12px;margin:10px 0;background:var(--card);border:1px solid var(--line);border-radius:20px;transition:transform .15s}.gcard:active{transform:scale(.98)}.gcard[hidden]{display:none}
.gcard b{display:block}.gcard small{display:block;color:var(--mut);font-size:.8rem}
.gav{display:inline-flex;align-items:center;justify-content:center;object-fit:cover;color:#fff;font-weight:800;flex:none}
.gj{font-size:.76rem;font-weight:700;padding:5px 12px;border-radius:99px;background:#ff6a1a22;color:var(--or2);white-space:nowrap}.gj.o{background:var(--or);color:#fff}
.fab{position:fixed;right:16px;bottom:calc(96px + env(safe-area-inset-bottom));z-index:35;width:56px;height:56px;border-radius:50%;padding:0;box-shadow:0 8px 24px #ff6a1a66}
.sheet{position:fixed;inset:0;z-index:90;background:#000a;display:flex;align-items:flex-end;justify-content:center}.sheet[hidden]{display:none}
.shp{width:100%;max-width:640px;max-height:90vh;overflow:auto;background:var(--card);border-radius:26px 26px 0 0;padding:10px 18px calc(22px + env(safe-area-inset-bottom));animation:up .28s cubic-bezier(.2,.9,.3,1)}@keyframes up{from{transform:translateY(100%)}}
.grab{display:block;width:44px;height:5px;border-radius:9px;background:var(--line);margin:4px auto 12px}
.gcover{height:128px;border-radius:0 0 26px 26px;background:linear-gradient(120deg,#ff6a1a,#ff3d71 60%,#6a3dff) center/cover;margin:-4px -16px 0}
.ghd2{display:flex;align-items:flex-end;gap:12px;margin-top:-34px;padding:0 2px;position:relative}.ghd2 .gav{border:4px solid var(--bg);box-shadow:0 6px 20px #0008}
.otype{display:inline-block;margin:6px 0 2px;padding:3px 12px;border-radius:99px;background:#ff6a1a22;color:var(--or2);font-weight:700;font-size:.8rem}
.badges{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px;margin-bottom:12px}
.bdg{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:10px 6px;text-align:center;opacity:.4;filter:grayscale(1)}.bdg.on{opacity:1;filter:none;border-color:#ff6a1a55}
.bdg span{font-size:1.6rem;display:block}.bdg small{font-size:.68rem;font-weight:700;display:block;margin-top:2px}
.zoom{position:fixed;inset:0;z-index:100;background:#000e;display:flex;align-items:center;justify-content:center;padding:10px}.zoom img{max-width:100%;max-height:100%;border-radius:8px}
"""

# ───────────────────────────── Templates ─────────────────────────────
POST_MACRO = r"""{% macro flag(kind, id, username) %}{% if me and me.username!=username %}<details class="more"><summary>{{ ic('dots',18) }}</summary><div class="mn" style="top:26px;right:0;min-width:220px"><form method="post" action="/signaler/{{ kind }}/{{ id }}">{{ csrf() }}<select name="reason" style="margin-bottom:6px">{% for r in REASONS %}<option>{{ r }}</option>{% endfor %}</select><button>{{ ic('shield',18) }} Signaler</button></form><form method="post" action="/bloquer/{{ username }}">{{ csrf() }}<button>{{ ic('lock',18) }} Bloquer {{ username }}</button></form></div></details>{% endif %}{% endmacro %}
{% macro post(p, full=False) %}<article class="post"><div class="ph">{{ ava(p.username,38) }}<div class="grow"><a href="/profil/{{ p.username }}"><b>{{ p.username }}</b></a><small>{{ ago(p.created) }}</small></div>
{% if me and me.id==p.user_id %}<form method="post" action="/supprimer/post/{{ p.id }}" onsubmit="return confirm('Supprimer ?')">{{ csrf() }}<button class="btn g s">{{ ic('trash',16) }}</button></form>{% else %}{{ flag('post',p.id,p.username) }}{% endif %}</div>
{% if p.body %}<p>{{ p.body }}</p>{% endif %}{% if p.has_img==2 %}<div class="gone">📷 Photo expirée (conservée {{ MEDIA_DAYS }} jours)</div>{% endif %}{% if p.has_img==1 %}<img class="pimg" src="/api/post/media/{{ p.id }}?v={{ p.created|replace('-','')|replace(' ','')|replace(':','') }}" alt="Photo" loading="lazy" onclick="zoom(this.src)">{% endif %}{% if p.np %}{{ pollbox(p.id) }}{% endif %}
{% if p.mal_id %}<a class="att" href="/serie/{{ p.mal_id }}">{% if p.m_img %}<img src="{{ p.m_img }}" loading="lazy" alt="">{% endif %}<div><b>{{ p.m_title }}</b><small class="mut" style="display:block">Voir la fiche →</small></div></a>{% endif %}
<div class="act"><form method="post" action="/aimer/{{ p.id }}">{{ csrf() }}<button class="{{ 'on' if p.liked }}">{{ ic('heart',19) }} {{ p.lc }}</button></form><a href="/post/{{ p.id }}">{{ ic('msg',19) }} {{ p.cc }}</a></div></article>{% endmacro %}
{% macro pollbox(pid) %}{% set pl = poll_of(pid) %}{% set show = pl.mine or not me %}<div class="poll{{ ' vs' if pl.opts|length==2 }}" id="poll{{ pid }}"><div class="pos">{% for o in pl.opts %}<button type="button" class="po{{ ' on' if o.id==pl.mine }}" {% if me %}onclick="vote({{ pid }},{{ o.id }})"{% else %}onclick="location.href='/connexion'"{% endif %}><i style="--p:{{ o.pct if show else 0 }}%"></i><span>{{ o.label }}</span>{% if show %}<b>{{ o.pct }}%</b>{% endif %}</button>{% endfor %}</div><small class="mut">{{ pl.total }} vote{{ 's' if pl.total>1 }}{% if me and not pl.mine %} · vote pour voir les résultats{% endif %}</small></div>{% endmacro %}
{% macro ptools(poll=True) %}<div class="ptools"><label class="ib" title="Ajouter une photo">{{ ic('image',22) }}<input type="file" class="pph" accept="image/*" hidden></label>{% if poll %}<button type="button" class="ib ppb" title="Créer un sondage">{{ ic('chart',22) }}</button>{% endif %}<span class="grow"></span><button class="btn s">Publier</button></div><div class="pprev" hidden></div>{% if poll %}<div class="ppoll" hidden><small class="mut">Écris ta question plus haut, puis 2 à 4 choix (2 choix = duel « versus »).</small><input name="opt" maxlength="40" placeholder="Choix 1"><input name="opt" maxlength="40" placeholder="Choix 2"><input name="opt" maxlength="40" placeholder="Choix 3 (facultatif)"><input name="opt" maxlength="40" placeholder="Choix 4 (facultatif)"></div>{% endif %}{% endmacro %}
{% macro tiles(data) %}<div class="grid">{% for m in data %}<a class="tile" href="/serie/{{ m.id }}"><img src="{{ m.img }}" loading="lazy" alt="">{% if m.score %}<small>★ {{ m.score }}</small>{% endif %}<b>{{ m.title }}</b></a>{% endfor %}</div>{% if not data %}<p class="mut" style="text-align:center">Aucun résultat — la source (Jikan) est peut-être occupée, réessaie.<br><small>{{ err }}</small> <a href="/diagnostic" style="color:var(--or)">Diagnostic</a></p>{% endif %}{% endmacro %}
{% macro item(icon, label, sub='', href='') %}{% if href %}<a href="{{ href }}" class="it"><span class="ico">{{ ic(icon,19) }}</span><span class="grow">{{ label }}{% if sub %}<small>{{ sub }}</small>{% endif %}</span>{{ ic('chev',18) }}</a>{% else %}<div class="it"><span class="ico">{{ ic(icon,19) }}</span><span class="grow">{{ label }}{% if sub %}<small>{{ sub }}</small>{% endif %}</span></div>{% endif %}{% endmacro %}
{% macro sw(name, on, label, sub='', icon='bell') %}<label class="it" style="margin:0"><span class="ico">{{ ic(icon,19) }}</span><span class="grow">{{ label }}{% if sub %}<small>{{ sub }}</small>{% endif %}</span><span class="sw"><input type="checkbox" name="{{ name }}" {{ 'checked' if on }}><i></i></span></label>{% endmacro %}
{% macro cd(eta) %}<div class="cd" data-eta="{{ eta }}"><div><b>00</b><small>Jours</small></div><div><b>00</b><small>Heures</small></div><div><b>00</b><small>Min</small></div><div><b>00</b><small>Sec</small></div></div>
<script>(function(){var e=document.currentScript.previousElementSibling,t=+e.dataset.eta,s=Date.now();function f(){var r=Math.max(0,t-Math.floor((Date.now()-s)/1000)),v=[Math.floor(r/86400),Math.floor(r%86400/3600),Math.floor(r%3600/60),r%60];e.querySelectorAll('b').forEach(function(b,i){b.textContent=String(v[i]).padStart(2,'0')})}f();setInterval(f,1000)})()</script>{% endmacro %}"""

T = {}
T["m.html"] = POST_MACRO
T["base.html"] = r"""<!doctype html><html lang="fr" data-theme="{{ me.theme if me else 'dark' }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#070b1a"><link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icon-192.png"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes"><title>{{ title }} · SenpaiX</title><style>{{ css|safe }}</style><script>{{ js|safe }}</script></head><body>
{% if not bare %}<header><div class="top">{% if back %}<a href="{{ back }}">{{ ic('back') }}</a>{% elif me %}<a href="/profil/{{ me.username }}">{{ ava(me.username,34) }}</a>{% endif %}
<a class="logo" href="/">Senpai<b>X</b></a><span class="sp"></span><a href="/recherche">{{ ic('search') }}</a>
{% if me %}<a class="b" href="/notifications">{{ ic('bell') }}{% if unread %}<i>{{ unread }}</i>{% endif %}</a>{% endif %}
<details class="menu"><summary>{{ ic('dots',24) }}</summary><div class="mn">{% if me %}<a href="/profil/{{ me.username }}">{{ ic('user',18) }} Mon profil</a><a href="/messages">{{ ic('msg',18) }} Messages</a><a href="/parametres">{{ ic('sliders',18) }} Paramètres</a>{% else %}<a href="/connexion">{{ ic('out',18) }} Connexion</a><a href="/inscription">{{ ic('user',18) }} Créer un compte</a>{% endif %}
{% if me %}<a href="/liste">{{ ic('star',18) }} Ma liste</a>{% endif %}{% if setup_open %}<a href="/installation">{{ ic('shield',18) }} Devenir administrateur</a>{% endif %}<a href="/catalogue">{{ ic('compass',18) }} Catalogue</a>{% if is_admin %}<a href="/admin">{{ ic('shield',18) }} Modération</a>{% endif %}<a href="#" onclick="installApp();return false">{{ ic('dl',18) }} Installer l'app</a><a href="/a-propos">{{ ic('info',18) }} À propos</a><a href="/confidentialite">{{ ic('lock',18) }} Confidentialité</a><a href="/conditions">{{ ic('shield',18) }} Conditions</a><a href="/diagnostic">{{ ic('sliders',18) }} Diagnostic</a>{% if me %}<form method="post" action="/deconnexion" onsubmit="try{caches.keys().then(function(k){k.forEach(function(x){caches.delete(x)})})}catch(e){}">{{ csrf() }}<button>{{ ic('out',18) }} Se déconnecter</button></form>{% endif %}</div></details></div></header>{% endif %}
<main>{% if is_admin and not durable %}<a class="flash err" href="/admin" style="display:block">⚠️ Stockage temporaire : les données seront effacées à la prochaine mise à jour. Touche ici pour voir comment corriger.</a>{% endif %}{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% block c %}{% endblock %}</main>
{% if not bare %}<nav class="tabs"><div>{% for h,i,l in [('/','home','Découverte'),('/communautes','users','Groupes'),('/quiz','game','Quiz'),('/catalogue','compass','Catalogue'),('/profil/'+me.username if me else '/connexion','user','Profil')] %}
<a href="{{ h }}" class="{{ 'on' if (path==h or (h!='/' and path.startswith(h[:6]))) }} {{ 'mid' if i=='game' }}">{{ ic(i) }}{{ l }}{% if i=='users' and gunread %}<b class="nb">{{ gunread if gunread<100 else '99+' }}</b>{% endif %}</a>{% endfor %}</div></nav>{% endif %}{% if me %}<script>setInterval(function(){if(document.hidden)return;fetch('/api/groupes/non-lus',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){var a=document.querySelector('.tabs a[href="/communautes"]');if(!a)return;var b=a.querySelector('.nb');if(d.n>0){if(!b){b=document.createElement('b');b.className='nb';a.appendChild(b)}b.textContent=d.n>99?'99+':d.n}else if(b)b.remove()}).catch(function(){})},40000)</script>{% endif %}<script>if('serviceWorker' in navigator)addEventListener('load',function(){navigator.serviceWorker.register('/sw.js').catch(function(){})});var DP;addEventListener('beforeinstallprompt',function(e){e.preventDefault();DP=e});
function installApp(){if(DP){DP.prompt();DP=null}else alert("Menu ⋮ du navigateur → « Installer l'application » ou « Ajouter à l'écran d'accueil ».")}</script><script>var CSRF="{{ session.get('csrf','') }}";
function openSheet(i){$(i).hidden=false;document.body.style.overflow='hidden'}
function closeSheet(e){e.hidden=true;document.body.style.overflow=''}
addEventListener('keydown',function(e){if(e.key==='Escape')document.querySelectorAll('.sheet').forEach(function(x){if(!x.hidden)closeSheet(x)})});
function zoom(src){var o=document.createElement('div');o.className='zoom';o.innerHTML='<img src="'+src+'" alt="">';o.onclick=function(){o.remove()};document.body.appendChild(o)}
function vote(p,o){fetch('/api/sondage/'+p+'/voter',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF':CSRF},body:JSON.stringify({o:o})}).then(function(r){return r.json()}).then(function(d){if(d.ok)$('poll'+p).outerHTML=d.html;else if(d.error)alert(d.error)}).catch(function(){alert('Connexion impossible.')})}
function crop(file,w,h,max){return new Promise(function(res,rej){var fr=new FileReader();fr.onerror=function(){rej(new Error('Lecture impossible'))};fr.onload=function(){var im=new Image();im.onerror=function(){rej(new Error('Image illisible'))};
 im.onload=function(){var c=document.createElement('canvas');c.width=w;c.height=h;var x=c.getContext('2d'),r=Math.max(w/im.width,h/im.height),sw=w/r,sh=h/r;x.fillStyle='#000';x.fillRect(0,0,w,h);x.drawImage(im,(im.width-sw)/2,(im.height-sh)/2,sw,sh,0,0,w,h);
  (function t(q){c.toBlob(function(b){if(!b)return rej(new Error('Conversion impossible'));if(b.size>max&&q>.35)return t(q-.1);b.size>max?rej(new Error('Image trop lourde')):res(b)},'image/jpeg',q)})(.85)};im.src=fr.result};fr.readAsDataURL(file)})}
(function(){function fit(file,max,lim){return new Promise(function(res,rej){var u=URL.createObjectURL(file),im=new Image();im.onerror=function(){URL.revokeObjectURL(u);rej(new Error('Image illisible.'))};
 im.onload=function(){URL.revokeObjectURL(u);var r=Math.min(1,max/Math.max(im.width,im.height)),w=Math.round(im.width*r),h=Math.round(im.height*r),c=document.createElement('canvas');c.width=w;c.height=h;var x=c.getContext('2d');x.fillStyle='#000';x.fillRect(0,0,w,h);x.drawImage(im,0,0,w,h);
  (function t(q){c.toBlob(function(b){if(!b)return rej(new Error('Conversion impossible.'));if(b.size>lim&&q>.4)return t(q-.1);b.size>lim?rej(new Error('Image trop lourde.')):res(b)},'image/jpeg',q)})(.85)};im.src=u})}
function clr(f){if(f._p){URL.revokeObjectURL(f._p.url);f._p=null}var v=f.querySelector('.pprev');if(v){v.hidden=true;v.innerHTML=''}}
function setp(f,b,m){clr(f);f._p={blob:b,mime:m,url:URL.createObjectURL(b)};var v=f.querySelector('.pprev');v.innerHTML='<img src="'+f._p.url+'" alt=""><button type="button" aria-label="Retirer">\u2715</button>';v.hidden=false;v.querySelector('button').onclick=function(){clr(f)}}
document.addEventListener('change',function(e){var i=e.target;if(!i.classList||!i.classList.contains('pph'))return;var f=i.files[0],fm=i.closest('form');i.value='';if(!f)return;
 if(f.type==='image/gif'&&f.size<=1500000)return setp(fm,f,'image/gif');fit(f,1280,560000).then(function(b){setp(fm,b,'image/jpeg')},function(x){alert(x.message)})});
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.ppb');if(!b)return;var p=b.closest('form').querySelector('.ppoll');p.hidden=!p.hidden;b.classList.toggle('on',!p.hidden)});
document.addEventListener('submit',function(e){var fm=e.target;if(!fm.classList||!fm.classList.contains('pform')||!fm._p)return;e.preventDefault();
 var q=new URLSearchParams(),el=fm.elements;q.set('t',(el.body.value||'').trim());['mal_id','m_title','m_img'].forEach(function(n){if(el[n]&&el[n].value)q.set(n,el[n].value)});
 [].forEach.call(fm.querySelectorAll('[name=opt]'),function(o){if(o.value.trim())q.append('o',o.value.trim())});
 var bt=fm.querySelector('.ptools .btn');bt.disabled=true;bt.textContent='Envoi\u2026';function back(m){bt.disabled=false;bt.textContent='Publier';alert(m)}
 var x=new XMLHttpRequest();x.open('POST','/api/publier?'+q.toString());x.setRequestHeader('Content-Type',fm._p.mime);x.setRequestHeader('X-CSRF',CSRF);
 x.onload=function(){var d={};try{d=JSON.parse(x.responseText)}catch(_){}if(d.ok)location.reload();else back(d.error||'Envoi impossible.')};x.onerror=function(){back('Connexion impossible.')};x.send(fm._p.blob)})})();
</script></body></html>"""

T["ev.html"] = r"""{% macro evcard(e) %}<a class="evc" href="/evenement/{{ e.id }}"><img src="/evt/{{ e.id }}?v={{ e.created|replace('-','')|replace(' ','')|replace(':','') }}" alt="" loading="lazy"><span class="evk">{{ EVT_KINDS.get(e.kind,'') }}</span><span class="evt"><b>{{ e.title }}</b>{% if e.day %}<small>📅 {{ fdate(e.day) }}</small>{% endif %}</span></a>{% endmacro %}"""

T["events.html"] = r"""{% extends "base.html" %}{% from "ev.html" import evcard %}{% set back='/' %}{% block c %}<h1 style="font-size:1.4rem">À la une</h1>
{% if rows %}<div class="evgrid">{% for e in rows %}{{ evcard(e) }}{% endfor %}</div>{% else %}<p class="mut" style="text-align:center;margin-top:30px">Rien à la une pour le moment.</p>{% endif %}
{% if past %}<h2>Terminés</h2><div class="evgrid" style="opacity:.75">{% for e in past %}{{ evcard(e) }}{% endfor %}</div>{% endif %}{% endblock %}"""

T["event.html"] = r"""{% extends "base.html" %}{% set back='/evenements' %}{% block c %}<img class="evhero" src="/evt/{{ e.id }}?v={{ e.created|replace('-','')|replace(' ','')|replace(':','') }}" alt="">
<div class="row" style="margin:12px 0 4px;flex-wrap:wrap"><span class="gj">{{ EVT_KINDS.get(e.kind,'') }}</span>{% if over %}<span class="gj o">Terminé</span>{% endif %}{% if e.hidden %}<span class="gj o">Masqué (visible par toi seul)</span>{% endif %}{% if e.day %}<small class="mut">📅 {{ fdate(e.day) }}{% if e.fin %} → {{ fdate(e.fin) }}{% endif %}</small>{% elif e.fin %}<small class="mut">Jusqu'au {{ fdate(e.fin) }}</small>{% endif %}</div>
<h1 style="font-size:1.4rem;margin:6px 0">{{ e.title }}</h1>{% if e.body %}<p style="white-space:pre-wrap">{{ e.body }}</p>{% endif %}
{% if e.link and not over %}<a class="btn w" href="{{ e.link }}" {% if e.link.startswith('https://') %}target="_blank" rel="noopener"{% endif %}>{{ 'Jouer' if e.kind=='jeu' else 'Ouvrir' }}</a>{% endif %}{% endblock %}"""

T["admin_events.html"] = r"""{% extends "adm.html" %}{% block a %}<h2>Ajouter un événement, un jeu ou une annonce</h2>
<div class="card"><label>Type</label><select id="ek"><option value="evenement">🎉 Événement</option><option value="jeu">🎮 Jeu</option><option value="annonce">📢 Annonce</option></select>
<label>Image (format 16:9 conseillé, ex. 1280 × 720)</label><button type="button" class="btn s g" onclick="$('ei').click()">{{ ic('image',16) }} Choisir une image</button><input type="file" id="ei" accept="image/*" hidden>
<div id="epw" hidden style="aspect-ratio:16/9;border-radius:14px;overflow:hidden;margin:10px 0 2px;background:#000"><img id="epv" alt="" style="width:100%;height:100%;object-fit:cover"></div><small id="esz" class="mut"></small>
<label>Titre</label><input id="et" maxlength="80" placeholder="Ex : Tournoi Quiz du week-end">
<label>Description (facultatif)</label><textarea id="ed" maxlength="600" style="min-height:84px" placeholder="Règles, récompenses, infos pratiques…"></textarea>
<label>Lien du bouton (facultatif)</label><input id="el" placeholder="/quiz ou https://…" autocomplete="off"><div class="row" style="flex-wrap:wrap;margin:6px 0">{% for l,h in [('Quiz','/quiz'),('Catalogue','/catalogue'),('Groupes','/communautes'),('Bandes-annonces','/bandes-annonces')] %}<button type="button" class="btn s g" onclick="$('el').value='{{ h }}'">{{ l }}</button>{% endfor %}</div>
<div class="row"><div class="grow"><label>Début</label><input type="date" id="edt"></div><div class="grow"><label>Fin (disparaît ensuite)</label><input type="date" id="efin"></div></div>
<div id="eerr" class="flash err" hidden></div><p><button type="button" class="btn w" id="eadd">Publier</button></p></div>
<h2>En ligne et masqués ({{ rows|length }})</h2>
{% for e in rows %}<div class="card row" style="align-items:center"><img src="/evt/{{ e.id }}?v={{ e.created|replace('-','')|replace(' ','')|replace(':','') }}" alt="" loading="lazy" style="width:110px;aspect-ratio:16/9;object-fit:cover;border-radius:10px;flex:none{{ ';opacity:.5' if e.st!='En ligne' }}">
<div class="grow" style="min-width:0"><b>{{ e.title }}</b><small class="mut" style="display:block">{{ EVT_KINDS.get(e.kind,'') }}{% if e.day %} · {{ fdate(e.day) }}{% endif %}{% if e.fin %} → {{ fdate(e.fin) }}{% endif %}</small><small style="display:block;color:{{ 'var(--or2)' if e.st=='En ligne' else 'var(--mut)' }}"><a href="/evenement/{{ e.id }}" style="color:inherit">{{ e.st }}</a></small></div>
<form method="post" action="/admin/action" style="display:flex;gap:6px;flex-wrap:wrap">{{ csrf() }}<input type="hidden" name="id" value="{{ e.id }}"><button class="btn s g" name="act" value="evt_up" aria-label="Monter" {% if loop.first %}disabled{% endif %}>▲</button><button class="btn s g" name="act" value="evt_down" aria-label="Descendre" {% if loop.last %}disabled{% endif %}>▼</button><button class="btn s g" name="act" value="evt_toggle">{{ 'Afficher' if e.hidden else 'Masquer' }}</button><button class="btn s g" name="act" value="evt_del" onclick="return confirm('Supprimer définitivement ?')">{{ ic('trash',14) }}</button></form></div>
{% else %}<p class="mut">Rien pour l'instant. Ajoute une image ci-dessus : elle apparaît aussitôt en haut de l'accueil.</p>{% endfor %}
<script>(function(){var blob=null,csrf="{{ session.get('csrf','') }}";
function er(m){var e=$('eerr');e.textContent=m;e.hidden=false}
function fit(file,max,lim){return new Promise(function(res,rej){var u=URL.createObjectURL(file),im=new Image();im.onerror=function(){URL.revokeObjectURL(u);rej(new Error('Image illisible.'))};
 im.onload=function(){URL.revokeObjectURL(u);var r=Math.min(1,max/Math.max(im.width,im.height)),w=Math.round(im.width*r),h=Math.round(im.height*r),c=document.createElement('canvas');c.width=w;c.height=h;var x=c.getContext('2d');x.fillStyle='#000';x.fillRect(0,0,w,h);x.drawImage(im,0,0,w,h);
  (function t(q){c.toBlob(function(b){if(!b)return rej(new Error('Conversion impossible.'));if(b.size>lim&&q>.4)return t(q-.1);b.size>lim?rej(new Error('Image trop lourde.')):res(b)},'image/jpeg',q)})(.85)};im.src=u})}
$('ei').onchange=function(){var f=this.files[0];this.value='';if(!f)return;$('eerr').hidden=true;fit(f,1280,600000).then(function(b){blob=b;$('epv').src=URL.createObjectURL(b);$('epw').hidden=false;$('esz').textContent='Image prête · '+Math.round(b.size/1000)+' Ko (recadrée en 16:9 sur l\'accueil)'},function(e){er(e.message)})};
$('eadd').onclick=function(){var t=$('et').value.trim();if(!blob)return er('Choisis une image.');if(!t)return er('Donne un titre.');$('eerr').hidden=true;
 var q=new URLSearchParams({c:$('ek').value,t:t,d:$('ed').value.trim(),l:$('el').value.trim(),dt:$('edt').value,fin:$('efin').value}),b=this;b.disabled=true;
 fetch('/api/admin/evenement?'+q,{method:'POST',headers:{'Content-Type':'image/jpeg','X-CSRF':csrf},body:blob,credentials:'same-origin'}).then(function(r){return r.json().then(function(d){if(!r.ok||!d.ok)throw new Error(d.error||'Envoi impossible ('+r.status+').');location.href='/admin/a-la-une'})}).catch(function(e){er(e.message);b.disabled=false})}})()</script>{% endblock %}"""

T["downloads.html"] = r"""{% extends "base.html" %}{% set back='/' %}{% block c %}<h1 style="font-size:1.4rem">Téléchargements</h1>
{% for d in rows %}<div class="card dlc"><div class="row" style="align-items:center;gap:12px">{% if d.has_icon %}<img class="dli" src="/dl/{{ d.id }}/icone?v={{ d.created|replace('-','')|replace(' ','')|replace(':','') }}" alt="" loading="lazy">{% else %}<span class="dli dlf">{{ PLATS.get(d.plat,'📎')[:2] }}</span>{% endif %}
<div class="grow" style="min-width:0"><b>{{ d.title }}</b><small class="mut" style="display:block">{{ PLATS.get(d.plat,'') }}{% if d.version %} · v{{ d.version }}{% endif %}{% if d.size %} · {{ d.size }}{% endif %}</small></div></div>
{% if d.body %}<p style="white-space:pre-wrap">{{ d.body }}</p>{% endif %}<a class="btn w" href="/telecharger/{{ d.id }}" target="_blank" rel="nofollow noopener">Télécharger</a><small class="mut" style="display:block;text-align:center;margin-top:6px">Lien externe : {{ host_of(d.url) }}</small></div>
{% else %}<p class="mut" style="text-align:center;margin-top:30px">Aucun téléchargement pour le moment.</p>{% endfor %}{% endblock %}"""

T["admin_downloads.html"] = r"""{% extends "adm.html" %}{% block a %}<h2>Ajouter un lien de téléchargement</h2>
<div class="card"><label>Nom</label><input id="dt" maxlength="80" placeholder="Ex : SenpaiX pour Android">
<label>Lien de téléchargement (https://…)</label><input id="du" type="url" placeholder="https://…" autocomplete="off">
<label>Plateforme</label><select id="dp">{% for k,v in PLATS.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
<div class="row"><div class="grow"><label>Version (facultatif)</label><input id="dv" maxlength="20" placeholder="1.2.0"></div><div class="grow"><label>Taille (facultatif)</label><input id="ds" maxlength="20" placeholder="24 Mo"></div></div>
<label>Description (facultatif)</label><textarea id="dd" maxlength="600" style="min-height:84px" placeholder="Nouveautés, comment l'installer…"></textarea>
<label>Icône (facultatif)</label><div class="row" style="align-items:center"><button type="button" class="btn s g" onclick="$('di').click()">{{ ic('image',16) }} Choisir une icône</button><input type="file" id="di" accept="image/*" hidden><img id="dpv" class="dli" alt="" hidden></div>
<div id="derr" class="flash err" hidden></div><p><button type="button" class="btn w" id="dadd">Ajouter</button></p></div>
<h2>Liens en ligne ({{ rows|length }})</h2>
{% for d in rows %}<div class="card row" style="align-items:center">{% if d.has_icon %}<img class="dli" src="/dl/{{ d.id }}/icone?v={{ d.created|replace('-','')|replace(' ','')|replace(':','') }}" alt="" loading="lazy" style="width:48px;height:48px">{% else %}<span class="dli dlf" style="width:48px;height:48px">{{ PLATS.get(d.plat,'📎')[:2] }}</span>{% endif %}
<div class="grow" style="min-width:0"><b>{{ d.title }}</b><small class="mut" style="display:block">{{ PLATS.get(d.plat,'') }} · {{ host_of(d.url) }}</small><small style="display:block;color:{{ 'var(--mut)' if d.hidden else 'var(--or2)' }}">{{ 'Masqué' if d.hidden else 'En ligne' }} · {{ d.hits }} clic{{ 's' if d.hits>1 }}</small></div>
<form method="post" action="/admin/action" style="display:flex;gap:6px;flex-wrap:wrap">{{ csrf() }}<input type="hidden" name="id" value="{{ d.id }}"><button class="btn s g" name="act" value="dl_up" aria-label="Monter" {% if loop.first %}disabled{% endif %}>▲</button><button class="btn s g" name="act" value="dl_down" aria-label="Descendre" {% if loop.last %}disabled{% endif %}>▼</button><button class="btn s g" name="act" value="dl_toggle">{{ 'Afficher' if d.hidden else 'Masquer' }}</button><button class="btn s g" name="act" value="dl_del" onclick="return confirm('Supprimer ce lien ?')">{{ ic('trash',14) }}</button></form></div>
{% else %}<p class="mut">Aucun lien pour l'instant. Ajoute-en un ci-dessus : il apparaît aussitôt dans la page Téléchargements.</p>{% endfor %}
<script>(function(){var blob=null,csrf="{{ session.get('csrf','') }}";
function er(m){var e=$('derr');e.textContent=m;e.hidden=false}
$('di').onchange=function(){var f=this.files[0];this.value='';if(!f)return;$('derr').hidden=true;var u=URL.createObjectURL(f),im=new Image();im.onerror=function(){URL.revokeObjectURL(u);er('Image illisible.')};
 im.onload=function(){URL.revokeObjectURL(u);var n=192,c=document.createElement('canvas');c.width=c.height=n;var x=c.getContext('2d'),s=Math.min(im.width,im.height);x.fillStyle='#000';x.fillRect(0,0,n,n);x.drawImage(im,(im.width-s)/2,(im.height-s)/2,s,s,0,0,n,n);
  c.toBlob(function(b){if(!b)return er('Conversion impossible.');blob=b;$('dpv').src=URL.createObjectURL(b);$('dpv').hidden=false},'image/jpeg',.85)};im.src=u};
$('dadd').onclick=function(){var t=$('dt').value.trim(),u=$('du').value.trim();if(!t)return er('Donne un nom.');if(!/^https:\/\/\S{4,}$/.test(u))return er('Le lien doit commencer par https:// (sans espace).');$('derr').hidden=true;
 var q=new URLSearchParams({t:t,u:u,p:$('dp').value,v:$('dv').value.trim(),s:$('ds').value.trim(),d:$('dd').value.trim()}),b=this;b.disabled=true;
 fetch('/api/admin/telechargement?'+q,{method:'POST',headers:{'Content-Type':'image/jpeg','X-CSRF':csrf},body:blob||'',credentials:'same-origin'}).then(function(r){return r.json().then(function(d){if(!r.ok||!d.ok)throw new Error(d.error||'Envoi impossible ('+r.status+').');location.href='/admin/telechargements'})}).catch(function(e){er(e.message);b.disabled=false})}})()</script>{% endblock %}"""

T["trl.html"] = r"""{% macro tlist(items, grid=False) %}<div class="{{ 'tgrid' if grid else 'tstrip' }}">{% for t in items %}<a class="tcard" href="https://www.youtube.com/watch?v={{ t.yt }}" target="_blank" rel="noopener" data-t="{{ t.title }}" data-m="{{ t.mal_id }}" onclick="return playTrailer('{{ t.yt }}',this)"><span class="tth"><img src="https://img.youtube.com/vi/{{ t.yt }}/mqdefault.jpg" loading="lazy" alt=""><i>▶</i></span><b>{{ t.title }}</b></a>{% endfor %}</div>{% endmacro %}
{% macro tplayer() %}<div class="sheet tvm" id="tvm" hidden onclick="if(event.target===this)closeT()"><div class="tbox"><div class="tfr" id="tfr"></div><div class="row" style="padding:10px 4px 2px"><b class="grow" id="tti" style="min-width:0"></b><a class="btn s" id="tfi" href="#" hidden>Voir la fiche</a><button type="button" class="btn s g" onclick="closeT()">Fermer</button></div></div></div>
<script>window.playTrailer=function(id,b){$('tfr').innerHTML='<iframe src="https://www.youtube-nocookie.com/embed/'+id+'?autoplay=1&rel=0&playsinline=1" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin" title="Bande-annonce"></iframe>';
$('tti').textContent=b.dataset.t;var a=$('tfi'),m=b.dataset.m;if(m&&m!='0'){a.href='/serie/'+m;a.hidden=false}else a.hidden=true;openSheet('tvm');return false};
window.closeT=function(){$('tfr').innerHTML='';closeSheet($('tvm'))};addEventListener('keydown',function(e){if(e.key==='Escape'&&!$('tvm').hidden)closeT()})</script>{% endmacro %}"""

T["trailers.html"] = r"""{% extends "base.html" %}{% from "trl.html" import tlist, tplayer with context %}{% set back='/' %}{% block c %}<h1 style="font-size:1.4rem">Bandes-annonces</h1>
{% if rows %}{{ tlist(rows, True) }}{{ tplayer() }}{% else %}<p class="mut" style="text-align:center;margin-top:30px">Aucune bande-annonce pour le moment.</p>{% endif %}{% endblock %}"""

T["admin_trailers.html"] = r"""{% extends "adm.html" %}{% block a %}<h2>Ajouter une bande-annonce</h2>
<form method="post" action="/admin/action" class="card">{{ csrf() }}<input type="hidden" name="act" value="trailer_add"><input type="hidden" name="mal_id" id="tm"><input type="hidden" name="m_title" id="tmt">
<label>Lien YouTube</label><input name="link" id="tl" placeholder="https://www.youtube.com/watch?v=…" required autocomplete="off">
<div id="tprev" class="tth" style="display:block;position:relative;border-radius:12px;overflow:hidden;aspect-ratio:16/9;margin:8px 0;background:#000" hidden><img id="tpi" alt="" style="width:100%;height:100%;object-fit:cover"></div>
<label>Titre affiché</label><input name="title" id="tt" maxlength="100" placeholder="Ex : Solo Leveling saison 2 — Bande-annonce" required>
<label>Série liée (facultatif : ajoute un bouton « Voir la fiche »)</label><input id="ts" type="search" placeholder="Rechercher un animé…" autocomplete="off"><div id="tres"></div>
<p id="tsel" class="mut" style="margin:6px 0" hidden></p>
<p><button class="btn w">Ajouter</button></p></form>
<h2>Bandes-annonces en ligne ({{ rows|length }})</h2>
{% for t in rows %}<div class="card row" style="align-items:center"><img src="https://img.youtube.com/vi/{{ t.yt }}/mqdefault.jpg" alt="" loading="lazy" style="width:110px;aspect-ratio:16/9;object-fit:cover;border-radius:10px;flex:none">
<div class="grow" style="min-width:0"><b>{{ t.title }}</b><small class="mut" style="display:block">{% if t.mal_id %}Série : <a href="/serie/{{ t.mal_id }}" style="color:var(--or2)">{{ t.stitle or ('#' ~ t.mal_id) }}</a> · {% endif %}<a href="https://www.youtube.com/watch?v={{ t.yt }}" target="_blank" rel="noopener" style="color:var(--or2)">Voir sur YouTube</a></small></div>
<form method="post" action="/admin/action" style="display:flex;gap:6px;flex-wrap:wrap">{{ csrf() }}<input type="hidden" name="id" value="{{ t.id }}"><button class="btn s g" name="act" value="trailer_up" aria-label="Monter" {% if loop.first %}disabled{% endif %}>▲</button><button class="btn s g" name="act" value="trailer_down" aria-label="Descendre" {% if loop.last %}disabled{% endif %}>▼</button><button class="btn s g" name="act" value="trailer_del" onclick="return confirm('Supprimer cette bande-annonce ?')">{{ ic('trash',14) }}</button></form></div>
{% else %}<p class="mut">Aucune pour l'instant. Colle un lien YouTube ci-dessus : la bande-annonce apparaît aussitôt en haut de l'accueil.</p>{% endfor %}
<script>(function(){function ytid(v){v=v.trim();if(/^[\w-]{11}$/.test(v))return v;var m=/(?:youtu\.be\/|youtube(?:-nocookie)?\.com\/(?:watch\?(?:[^#]*&)?v=|embed\/|shorts\/|live\/|v\/))([\w-]{11})(?![\w-])/.exec(v);return m?m[1]:null}
$('tl').oninput=function(){var y=ytid(this.value);$('tprev').hidden=!y;if(y)$('tpi').src='https://img.youtube.com/vi/'+y+'/mqdefault.jpg'};
var st;$('ts').oninput=function(){clearTimeout(st);var v=this.value.trim();if(v.length<2){$('tres').innerHTML='';return}
 st=setTimeout(function(){L('search',{q:v}).then(function(l){$('tres').innerHTML=l.slice(0,6).map(function(m,i){return '<button type="button" class="rres" data-i="'+i+'"><img src="'+esc(m.img)+'" alt=""><b>'+esc(m.title)+'</b></button>'}).join('')||'<p class="mut">Aucun résultat.</p>';
 [].forEach.call($('tres').querySelectorAll('.rres'),function(b){b.onclick=function(){var m=l[+b.dataset.i];$('tm').value=m.id;$('tmt').value=m.title;if(!$('tt').value)$('tt').value=m.title+' — Bande-annonce';$('tsel').textContent='Série liée : '+m.title;$('tsel').hidden=false;$('tres').innerHTML='';$('ts').value=''}})},function(e){$('tres').innerHTML='<p class="mut">Recherche impossible ('+esc(e.message)+').</p>'})},400)}})()</script>{% endblock %}"""

T["home.html"] = r"""{% extends "base.html" %}{% from "m.html" import post, cd, ptools with context %}{% from "trl.html" import tlist, tplayer with context %}{% from "ev.html" import evcard %}{% block c %}
<div class="hero">L'univers<em>Otaku</em>autrement.</div>
<div class="stories" id="stories"></div>
<div class="pills"><a class="pill {{ 'on' if tab=='decouverte' }}" href="/">{{ ic('compass',16) }} Découverte</a><a class="pill {{ 'on' if tab=='suivis' }}" href="/?t=suivis">{{ ic('star',16) }} Mes séries</a><a class="pill" href="/catalogue?t=planning">{{ ic('home',16) }} Planning</a><a class="pill" href="/quiz">{{ ic('game',16) }} Quiz</a></div>
{% if events %}<div class="row" style="margin:6px 0 0"><h2 class="grow" style="margin:10px 0">À la une</h2><a href="/evenements" style="font-size:.8rem;color:var(--or2)">Tout voir ›</a></div><div class="evstrip">{% for e in events %}{{ evcard(e) }}{% endfor %}</div>{% endif %}
<a class="promo" href="/quiz"><span class="lg">TKN</span><span><b>Le prochain tournoi solo arrive bientôt</b><small>Démarrage dans {{ (eta//3600) }}h {{ (eta%3600//60) }}m — 10 questions, 15 s chacune</small></span></a>
{% if me %}<form method="post" action="/publier" class="card pform">{{ csrf() }}<div class="row">{{ ava(me.username,36) }}<textarea name="body" maxlength="1000" placeholder="Partage ton avis, un spoiler (avec précaution)…" style="min-height:52px"></textarea></div>{{ ptools() }}</form>
{% else %}<div class="card" style="text-align:center">Rejoins la communauté pour publier et jouer.<br><a class="btn s" style="margin-top:8px" href="/inscription">Créer un compte</a></div>{% endif %}
{% if trailers %}<div class="row" style="margin-top:6px"><h2 class="grow" style="margin:10px 0">Bandes-annonces</h2><a href="/bandes-annonces" style="font-size:.8rem;color:var(--or2)">Tout voir ›</a></div>{{ tlist(trailers) }}{{ tplayer() }}{% endif %}
{% if dlcount %}<a class="card row" href="/telechargements" style="align-items:center;margin:10px 0;color:var(--text)"><span class="dli dlf" style="width:44px;height:44px;font-size:1.3rem">📥</span><div class="grow"><b>Téléchargements</b><small class="mut" style="display:block">{{ dlcount }} lien{{ 's' if dlcount>1 }} disponible{{ 's' if dlcount>1 }}</small></div><span class="mut">›</span></a>{% endif %}
{% if watch %}<h2>Tes prochains épisodes</h2><div class="stories" id="nxt">{{ '' }}</div><script>$('nxt').innerHTML=skC(4);NX({{ watch|tojson }}).then(function(o){var a=Object.keys(o).map(function(k){return Object.assign({id:k},o[k])}).sort(function(x,y){return x.at-y.at});
$('nxt').innerHTML=a.length?a.map(function(m){return'<a class="st" style="width:104px" href="/serie/'+m.id+'"><img src="'+esc(m.img)+'" alt="" loading="lazy" style="border-radius:14px;aspect-ratio:2/3;border:0;object-fit:cover;width:100%"><span><b style="color:var(--or2)">Ép. '+m.ep+'</b> · '+dur(m.at-Date.now()/1000)+'</span></a>'}).join(''):'<p class="mut" style="font-size:.85rem">Aucun épisode à venir pour tes séries.</p>'}).catch(function(e){fail($('nxt'),e)})</script>{% endif %}
<h2>Fil de la communauté</h2>{% for p in posts %}{{ post(p) }}{% else %}<p class="mut" style="text-align:center;margin-top:24px">{{ 'Ajoute des séries en favori pour voir leurs discussions.' if tab=='suivis' else 'Aucun post pour le moment. Sois le premier !' }}</p>{% endfor %}
<h2>À l'affiche cette saison</h2><div class="stories" id="airing"></div>
<script>$('stories').innerHTML=skC(7);$('airing').innerHTML=skC(5);
L('trend').then(function(l){$('stories').innerHTML=rings(l.slice(0,14))}).catch(function(e){fail($('stories'),e)});
L('saison').then(function(l){$('airing').innerHTML=posters(l.slice(0,12))}).catch(function(e){fail($('airing'),e)});</script>{% endblock %}"""

T["search.html"] = r"""{% extends "base.html" %}{% block c %}
<form><div class="row"><input name="q" id="q" value="{{ q }}" placeholder="Animé, manga, membre…" autocomplete="off" autofocus><button class="btn">{{ ic('search',18) }}</button></div></form>
{% if us %}<h2>Membres</h2><div class="grp">{% for u in us %}<a class="it" href="/profil/{{ u.username }}">{{ ava(u.username,36) }}<span class="grow">{{ u.username }}<small>{{ u.bio[:60] }}</small></span></a>{% endfor %}</div>{% endif %}
<div id="res"></div>
<script>var tk=0,tm;function rec(){try{return JSON.parse(localStorage.getItem('rs'))||[]}catch(e){return[]}}
function home(){var r=rec();$('res').innerHTML=r.length?'<h2>Récent</h2><div class="pills" style="flex-wrap:wrap">'+r.map(function(x){return'<a class="pill" href="/recherche?q='+encodeURIComponent(x)+'">'+esc(x)+'</a>'}).join('')+'</div>':''}
function run(q){q=q.trim();var t=++tk;if(q.length<2){home();return}$('res').innerHTML=skT(6);
 L('search',{q:q}).then(function(l){if(t!=tk)return;$('res').innerHTML=l.length?'<h2>Animés</h2>'+tiles(l.slice(0,18)):'<p class="mut" style="text-align:center;padding:14px">Aucun résultat.<br><small>Essaie le titre en anglais ou en rōmaji, ou un mot du titre (ex : « Slime »).</small></p>';
  if(l.length){var r=rec().filter(function(x){return x!=q});r.unshift(q);try{localStorage.setItem('rs',JSON.stringify(r.slice(0,8)))}catch(e){}}}).catch(function(e){if(t==tk)fail($('res'),e)})}
$('q').oninput=function(){clearTimeout(tm);var v=this.value;tm=setTimeout(function(){run(v)},450)};run($('q').value);</script>{% endblock %}"""

T["catalog.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Catalogue</h1>
<div class="pills">{% for k,l in [('saison','En cours'),('explore','Explorer'),('top','Top'),('planning','Planning'),('manga','Mangas')] %}<a class="pill {{ 'on' if t==k }}" href="/catalogue?t={{ k }}">{{ l }}</a>{% endfor %}</div>
{% if t=='planning' %}<div class="pills">{% for k,l in [('monday','Lun'),('tuesday','Mar'),('wednesday','Mer'),('thursday','Jeu'),('friday','Ven'),('saturday','Sam'),('sunday','Dim')] %}<a class="pill {{ 'on' if day==k }}" href="/catalogue?t=planning&j={{ k }}">{{ l }}</a>{% endfor %}</div>{% endif %}
{% if t=='explore' %}<form class="card" style="display:grid;grid-template-columns:1fr 1fr;gap:8px"><input type="hidden" name="t" value="explore">
<select name="g"><option value="">Tous les genres</option>{% for x in GENRES %}<option {{ 'selected' if g==x }}>{{ x }}</option>{% endfor %}</select>
<select name="y"><option value="">Toutes les années</option>{% for x in range(2026,1979,-1) %}<option {{ 'selected' if y==x|string }}>{{ x }}</option>{% endfor %}</select>
<select name="so">{% for k,l in SORTS.items() %}<option value="{{ k }}" {{ 'selected' if so==k }}>{{ l }}</option>{% endfor %}</select>
<select name="mn"><option value="">Toute note</option>{% for x in range(9,4,-1) %}<option value="{{ x }}" {{ 'selected' if mn==x|string }}>≥ {{ x }}/10</option>{% endfor %}</select>
<button class="btn" style="grid-column:1/-1">Appliquer</button></form>{% endif %}
<div id="res" class="grid"></div><div id="mo" class="mut" style="text-align:center;padding:18px;font-size:.8rem">Chargement…</div><p class="mut" style="font-size:.72rem;text-align:center">Données : AniList / MyAnimeList</p>
<script>var K='{{ t }}',pg=1,busy=0,done=0,seen={},G=$('res'),O={day:'{{ day }}',genre:{{ g|tojson }},year:{{ y|tojson }},sort:'{{ so }}',min:{{ mn|tojson }}};
G.innerHTML=skI(9);
function add(l){var h=l.filter(function(m){if(seen[m.id])return 0;seen[m.id]=1;return 1});if(!h.length){done=1;return}G.insertAdjacentHTML('beforeend',tileItems(h,K))}
function more(){if(busy||done)return;busy=1;O.page=pg;L(K,O).then(function(l){if(pg==1)G.innerHTML='';add(l);if(K=='planning'||l.length<8)done=1;if(pg==1&&!l.length)G.innerHTML='<p class="mut" style="grid-column:1/-1;text-align:center">Aucun résultat pour ces filtres.</p>';pg++;busy=0;if(done)$('mo').hidden=true})
 .catch(function(e){if(pg==1)fail(G,e);done=1;busy=0;$('mo').hidden=true})}
if('IntersectionObserver' in window)new IntersectionObserver(function(e){if(e[0].isIntersecting)more()},{rootMargin:'500px'}).observe($('mo'));more();</script>{% endblock %}"""

T["media.html"] = r"""{% extends "base.html" %}{% from "m.html" import post, ptools with context %}{% block c %}
<div id="info"><div class="sk" style="height:190px"></div></div>
<p id="nxe" style="color:var(--or2);font-weight:700;margin:10px 0 0"></p>
{% if me %}<form method="post" action="/liste/{{ mid }}" class="card">{{ csrf() }}<input type="hidden" name="title" id="ft"><input type="hidden" name="img" id="fi"><input type="hidden" name="total" id="fe"><input type="hidden" name="kind" value="{{ k }}">
<div class="row"><select name="status">{% for key,l in STATUSES.items() %}<option value="{{ key }}" {{ 'selected' if cur and cur.status==key }}>{{ l }}</option>{% endfor %}</select><select name="score"><option value="0">Ma note</option>{% for i in range(10,0,-1) %}<option value="{{ i }}" {{ 'selected' if cur and cur.score==i }}>★ {{ i }}/10</option>{% endfor %}</select></div>
<div class="row" style="margin-top:8px"><input type="number" name="ep" min="0" value="{{ cur.ep if cur else 0 }}" aria-label="Épisode vu" style="max-width:110px"><button class="btn grow">{{ ic('star',18) }} {{ 'Mettre à jour' if cur else 'Ajouter à ma liste' }}</button></div></form>
{% if cur %}<form method="post" action="/liste/{{ mid }}">{{ csrf() }}<input type="hidden" name="action" value="remove"><button class="btn g s">Retirer de ma liste</button></form>{% endif %}
{% else %}<a class="btn w" style="margin:12px 0" href="/connexion?next=/serie/{{ mid }}">Connecte-toi pour suivre cette série</a>{% endif %}
<a id="tr" class="btn g w" target="_blank" rel="noopener" style="margin-top:8px" hidden>Bande-annonce</a>
<div class="card"><p id="syn" style="margin:0;white-space:pre-wrap" class="mut">Chargement…</p></div>
<div id="chars"></div>
<h2>En parler</h2>{% if me %}<form method="post" action="/publier" class="card pform">{{ csrf() }}<input type="hidden" name="mal_id" value="{{ mid }}"><input type="hidden" name="m_title" id="pt"><input type="hidden" name="m_img" id="pi"><textarea name="body" placeholder="Ton avis…" maxlength="1000"></textarea>{{ ptools() }}</form>{% endif %}
{% for p in posts %}{{ post(p) }}{% endfor %}
<script>M({{ mid }},'{{ k }}').then(function(m){document.title=m.title+' · SenpaiX';
$('info').innerHTML='<div class="row" style="align-items:flex-start"><img src="'+esc(m.img)+'" alt="" style="width:130px;border-radius:16px"><div class="grow"><h1 style="font-size:1.2rem">'+esc(m.title)+'</h1><p class="mut" style="margin:0 0 8px">'+esc(m.type||'')+' · '+(m.year||'?')+' · '+(m.eps||'?')+' ép./ch.<br>'+esc(m.genres)+(m.studio?'<br>'+esc(m.studio):'')+'</p>'+(m.score?'<b style="color:var(--or2)">★ '+m.score+'</b>':'')+'</div></div>';
if(m.next)$('nxe').textContent='📅 Épisode '+m.next.episode+' dans '+dur(m.next.airingAt-Date.now()/1000);
$('syn').textContent=m.syn||'Pas de synopsis.';if($('ft')){$('ft').value=m.title;$('fi').value=m.img;$('fe').value=m.eps||0}if($('pt')){$('pt').value=m.title;$('pi').value=m.img}
if(m.trailer){$('tr').href=m.trailer;$('tr').hidden=false}
if(m.chars&&m.chars.length)$('chars').innerHTML='<h2>Personnages</h2><div class="stories">'+m.chars.map(function(c){return'<div class="st"><div class="ring"><img src="'+esc(c.img)+'" alt="" loading="lazy"></div><span>'+esc(c.title)+'</span></div>'}).join('')+'</div>'})
.catch(function(e){fail($('info'),e);$('syn').textContent=''});</script>{% endblock %}"""

T["post.html"] = r"""{% extends "base.html" %}{% from "m.html" import post, flag with context %}{% block c %}{{ post(p) }}<h2>Commentaires</h2>
{% for c in cm %}<div class="post"><div class="ph">{{ ava(c.username,30) }}<div class="grow"><b>{{ c.username }}</b><small>{{ ago(c.created) }}</small></div>{% if me and me.id==c.user_id %}<form method="post" action="/supprimer/comment/{{ c.id }}">{{ csrf() }}<button class="btn g s">{{ ic('trash',14) }}</button></form>{% else %}{{ flag('comment',c.id,c.username) }}{% endif %}</div><p>{{ c.body }}</p></div>{% endfor %}
{% if me %}<form method="post" class="row" style="margin-top:12px">{{ csrf() }}<input name="body" maxlength="500" placeholder="Ajouter un commentaire…" required><button class="btn">{{ ic('send',18) }}</button></form>{% endif %}{% endblock %}"""

T["communities.html"] = r"""{% extends "base.html" %}{% block c %}<div class="hero" style="font-size:1.7rem">Rencontre des groupes de<em style="font-size:2rem">passionnés !</em></div>
<div class="gtools"><input id="gq" type="search" placeholder="Rechercher un groupe…" autocomplete="off"></div>
<div class="pills"><a class="pill on" href="#" data-f="all">Tous</a>{% if me %}<a class="pill" href="#" data-f="mine">{{ ic('users',16) }} Mes groupes</a>{% endif %}</div>
<div id="glist">{% for c in rows %}<a class="gcard" href="/communaute/{{ c.id }}" data-q="{{ (c.name ~ ' ' ~ c.topic)|lower }}" data-j="{{ 1 if c.joined else 0 }}">{{ gav(c,56) }}<div class="grow" style="min-width:0"><b>{{ c.name }}</b>{% if c.topic %}<small>{{ c.topic }}</small>{% endif %}<small>👥 {{ c.n }} membre{{ 's' if c.n>1 }}{% if c.la %} · 💬 {{ ago(c.la) }}{% endif %}</small></div>{% if c.joined %}{% set gx = gun.get(c.id) %}{% if gx and gx[1] %}<span title="Sourdine">🔕</span>{% elif gx and gx[0] %}<span class="gn">{{ gx[0] if gx[0]<100 else '99+' }}</span>{% endif %}<span class="gj">Membre</span>{% else %}<span class="gj o">Voir</span>{% endif %}</a>
{% else %}<p class="mut" style="text-align:center">Aucun groupe. Crée le premier !</p>{% endfor %}</div>
<p id="gnone" class="mut" style="text-align:center" hidden>Aucun groupe ne correspond.</p>
{% if me %}<button class="btn fab" aria-label="Créer un groupe" onclick="openSheet('nsheet')">{{ ic('plus',26) }}</button>
<div class="sheet" id="nsheet" hidden onclick="if(event.target===this)closeSheet(this)"><div class="shp"><i class="grab"></i><h2 style="margin:0 0 4px">Créer un groupe</h2><p class="mut" style="margin:0 0 8px">Tu pourras ajouter une photo et une bannière ensuite.</p><form method="post">{{ csrf() }}<label>Nom</label><input name="name" maxlength="40" required><label>Thème (ex : One Piece)</label><input name="topic" maxlength="40"><label>Description</label><textarea name="descr" maxlength="300" style="min-height:84px"></textarea><p><button class="btn w">Créer le groupe</button></p></form></div></div>{% endif %}
<script>(function(){var f='all',q=$('gq');function run(){var v=q.value.trim().toLowerCase(),n=0;document.querySelectorAll('#glist .gcard').forEach(function(c){var ok=(!v||c.dataset.q.indexOf(v)>-1)&&(f=='all'||c.dataset.j=='1');c.hidden=!ok;if(ok)n++});$('gnone').hidden=n>0}
q.oninput=run;document.querySelectorAll('.pills [data-f]').forEach(function(b){b.onclick=function(e){e.preventDefault();f=b.dataset.f;document.querySelectorAll('.pills [data-f]').forEach(function(x){x.classList.toggle('on',x==b)});run()}})})()</script>{% endblock %}"""

T["c.html"] = r"""{% macro rxbar(pid, counts, mine, joined) %}{% for e,n in counts %}<button type="button" class="rch{{ ' on' if e==mine }}" {% if joined %}onclick="react({{ pid }},'{{ e }}')"{% else %}disabled{% endif %}>{{ e }} <b>{{ n }}</b></button>{% endfor %}{% if joined %}<details class="rpk"><summary>{{ ic('smile',16) }}</summary><div class="rrow">{% for e in REACTS %}<button type="button" onclick="react({{ pid }},'{{ e }}')">{{ e }}</button>{% endfor %}</div></details>{% endif %}{% endmacro %}
{% macro cmsg(p, rc, rm, joined, mod, pinid) %}{% set mine = me and me.id==p.user_id %}{% set src = '/api/groupe/media/' ~ p.id ~ '?v=' ~ (p.created|replace('-','')|replace(' ','')|replace(':','')) %}
<div class="cm{{ ' mine' if mine }}" id="m{{ p.id }}" data-id="{{ p.id }}" data-who="{{ 'toi' if mine else p.username }}" data-snip="{{ snip(p.body, p.mk, p.m_title) }}">{% if not mine %}<a href="/profil/{{ p.username }}">{{ ava(p.username,32) }}</a>{% endif %}<div class="cb">{% if not mine %}<a class="cn" href="/profil/{{ p.username }}">{{ p.username }}</a>{% endif %}
{% if p.rid %}{% if p.qname %}<a class="qt" href="#m{{ p.rid }}" onclick="return goto({{ p.rid }})"><b>{{ p.qname }}</b><span>{{ snip(p.qbody, p.qmk, p.qtitle) }}</span></a>{% else %}<div class="qt"><span>Message supprimé</span></div>{% endif %}{% endif %}
{% if p.mk=='image' %}<img class="cimg" src="{{ src }}" alt="Photo" loading="lazy" onclick="zoom(this.src)">{% elif p.mk=='video' %}<video class="cvid" src="{{ src }}#t=0.1" controls playsinline preload="metadata"></video>{% elif p.mk=='audio' %}<audio class="caud" src="{{ src }}" controls preload="none"></audio>{% elif p.mk=='expire' %}<div class="gone">📎 Média expiré (conservé {{ MEDIA_DAYS }} jours)</div>{% endif %}
{% if p.mal_id %}<a class="att" href="/serie/{{ p.mal_id }}">{% if p.m_img %}<img src="{{ p.m_img }}" loading="lazy" alt="">{% endif %}<div><b>{{ p.m_title }}</b><small class="mut" style="display:block">Voir la fiche →</small></div></a>{% endif %}
{% if p.body %}<p>{{ p.body|ment }}</p>{% endif %}
<div class="cf"><small>{% if p.id==pinid %}📌 {% endif %}{{ ago(p.created) }}</small><div class="rx" id="rx{{ p.id }}">{{ rxbar(p.id, rc.get(p.id,[]), rm.get(p.id,''), joined) }}</div>
{% if joined %}<button type="button" class="rpl" onclick="replyTo({{ p.id }})" aria-label="Répondre">{{ ic('reply',16) }}</button>{% endif %}
{% if me %}<details class="more cmore"><summary>{{ ic('dots',16) }}</summary><div class="mn cmn">{% if mod %}<form method="post" action="/groupe/{{ p.cid }}/epingler/{{ p.id }}">{{ csrf() }}<button>{{ ic('pin',18) }} {{ 'Désépingler' if p.id==pinid else 'Épingler' }}</button></form>{% endif %}{% if mine %}<form method="post" action="/supprimer/cpost/{{ p.id }}" onsubmit="return confirm('Supprimer ce message ?')">{{ csrf() }}<button>{{ ic('trash',18) }} Supprimer</button></form>{% else %}{% if mod %}<form method="post" action="/supprimer/cpost/{{ p.id }}" onsubmit="return confirm('Supprimer ce message en tant que modérateur ?')">{{ csrf() }}<button>{{ ic('trash',18) }} Supprimer (modération)</button></form>{% endif %}<form method="post" action="/signaler/cpost/{{ p.id }}">{{ csrf() }}<select name="reason" style="margin-bottom:6px">{% for r in REASONS %}<option>{{ r }}</option>{% endfor %}</select><button>{{ ic('shield',18) }} Signaler</button></form><form method="post" action="/bloquer/{{ p.username }}">{{ csrf() }}<button>{{ ic('lock',18) }} Bloquer {{ p.username }}</button></form>{% endif %}</div></details>{% endif %}</div></div></div>{% endmacro %}"""
T["cfeed.html"] = r"""{% from "c.html" import cmsg with context %}{% for p in posts %}{{ cmsg(p, rc, rm, joined, mod, pinid) }}{% endfor %}"""
T["pollx.html"] = r"""{% from "m.html" import pollbox with context %}{{ pollbox(pid) }}"""
T["crx.html"] = r"""{% from "c.html" import rxbar with context %}{{ rxbar(pid, counts, mine, True) }}"""

T["gmembers.html"] = r"""{% extends "base.html" %}{% set back='/communaute/' ~ c.id %}{% block c %}<h1 style="font-size:1.3rem">Membres · {{ c.name }}</h1>
<div class="grp">{% for m in rows %}<div class="it">{{ ava(m.username,40) }}<a class="grow" href="/profil/{{ m.username }}"><b>{{ m.username }}</b>{% if m.id==c.creator_id %} <span title="Créateur">👑</span>{% endif %}</a>{% if mod and m.id!=c.creator_id and (not me or m.id!=me.id) %}<form method="post" action="/groupe/{{ c.id }}/retirer/{{ m.id }}" onsubmit="return confirm('Retirer {{ m.username }} du groupe ?')">{{ csrf() }}<button class="btn s g">Retirer</button></form>{% endif %}</div>{% endfor %}</div>
{% if kicked %}<h2>Membres retirés</h2><div class="grp">{% for m in kicked %}<div class="it">{{ ava(m.username,40) }}<span class="grow">{{ m.username }}</span><form method="post" action="/groupe/{{ c.id }}/readmettre/{{ m.id }}">{{ csrf() }}<button class="btn s g">Réadmettre</button></form></div>{% endfor %}</div>{% endif %}{% endblock %}"""

T["community.html"] = r"""{% extends "base.html" %}{% set back='/communautes' %}{% block c %}
{% if joined %}<style>.tabs{display:none}</style>{% endif %}
<script>window.zoom=function(src){var o=document.createElement('div');o.className='zoom';o.innerHTML='<img src="'+src+'" alt="">';o.onclick=function(){o.remove()};document.body.appendChild(o)};
window.goto=function(id){var e=$('m'+id);if(!e)return true;e.scrollIntoView({behavior:'smooth',block:'center'});e.classList.add('hl');setTimeout(function(){e.classList.remove('hl')},1700);return false};</script>
<div class="gcover" {% if c.cv %}style="background-image:url(/g/{{ c.id }}/cover?v={{ c.cv }})"{% endif %}></div>
<div class="ghd2">{{ gav(c,84) }}<div class="grow" style="min-width:0;padding-bottom:4px"><h1 style="margin:0;font-size:1.3rem">{{ c.name }}</h1><small class="mut">{% if c.topic %}{{ c.topic }} · {% endif %}<a href="/communaute/{{ c.id }}/membres" style="color:var(--or2)">{{ nmem }} membre{{ 's' if nmem>1 }} ›</a></small></div></div>
{% if c.descr %}<p class="mut" style="margin:10px 2px 0">{{ c.descr }}</p>{% endif %}<small class="mut" style="display:block;margin:6px 2px 0">📎 Photos, vidéos et vocaux conservés {{ MEDIA_DAYS }} jours</small>
<div class="row" style="margin:12px 0 4px;flex-wrap:wrap">{% if me and not joined %}<form method="post" style="flex:1 1 200px">{{ csrf() }}<input type="hidden" name="action" value="join"><button class="btn w" style="white-space:nowrap">Rejoindre le groupe</button></form>{% elif joined %}<span class="gj" style="padding:9px 16px">✓ Membre</span>{% elif not me %}<a class="btn grow" href="/connexion?next=/communaute/{{ c.id }}" style="text-align:center">Connecte-toi pour participer</a>{% endif %}
{% if can_edit %}<button type="button" class="btn s g" onclick="openSheet('esheet')">{{ ic('edit',16) }} Personnaliser</button>{% endif %}<span class="grow"></span>
{% if joined %}<details class="more"><summary>{{ ic('dots',22) }}</summary><div class="mn"><form method="post" action="/groupe/{{ c.id }}/sourdine">{{ csrf() }}<button>{{ '🔔 Réactiver les alertes' if muted else '🔕 Mettre en sourdine' }}</button></form><form method="post">{{ csrf() }}<input type="hidden" name="action" value="join"><button onclick="return confirm('Quitter le groupe ?')">{{ ic('out',18) }} Quitter le groupe</button></form></div></details>{% endif %}</div>
<div class="row" style="margin:8px 0 10px">{% for m in mem %}<a href="/profil/{{ m.username }}">{{ ava(m.username,30) }}</a>{% endfor %}<a href="/communaute/{{ c.id }}/membres" class="mut" style="font-size:.8rem;margin-left:4px">Tous les membres ›</a></div>
{% if pin %}<a class="pinb" href="#m{{ pin.id }}" onclick="return goto({{ pin.id }})">{{ ic('pin',18) }}<div><b>Message épinglé · {{ pin.username }}</b><span>{{ snip(pin.body, pin.mk, pin.m_title) }}</span></div></a>{% endif %}
{% if more %}<p style="text-align:center"><button type="button" class="btn g s" id="older">Messages plus anciens</button></p>{% endif %}
<div id="msgs">{% include "cfeed.html" %}</div>
<p id="empty" class="mut" style="text-align:center;margin:30px 0" {% if posts %}hidden{% endif %}>Aucun message pour le moment. Lance la conversation !</p>
{% if joined %}<div class="cbar" id="cbar"><div>
<div id="cerr" class="flash err" hidden></div><div id="tray" class="tray" hidden></div><div id="rcl" class="tray" hidden></div><div id="sug" class="sug" hidden></div>
<div id="rec" class="rec" hidden><i class="dot"></i><b id="rect">0:00</b><span class="grow mut">Enregistrement…</span><button type="button" class="btn g s" id="recx">Annuler</button><button type="button" class="btn s" id="recok">{{ ic('send',16) }} Envoyer</button></div>
<div id="att" class="attp" hidden><label class="ai"><span>{{ ic('image',24) }}</span>Photo<input type="file" id="fimg" accept="image/*" hidden></label><label class="ai"><span>{{ ic('video',24) }}</span>Vidéo<input type="file" id="fvid" accept="video/*" hidden></label><button type="button" class="ai" onclick="$('att').hidden=true;openSheet('ssheet');$('sq').focus()"><span>{{ ic('tv',24) }}</span>Série</button></div>
<form id="cf" method="post" class="crow">{{ csrf() }}<input type="hidden" name="action" value="post">
<button type="button" class="ib" id="plus" aria-label="Joindre une photo, une vidéo ou une série">{{ ic('plus',24) }}</button>
<textarea name="body" id="cbody" rows="1" maxlength="800" placeholder="Écris au groupe…"></textarea>
<button type="button" class="ib" id="mic" title="Message vocal" hidden>{{ ic('mic',22) }}</button>
<button class="send" id="csend" aria-label="Envoyer">{{ ic('send',20) }}</button></form></div></div>
<div class="sheet" id="ssheet" hidden onclick="if(event.target===this)closeSheet(this)"><div class="shp"><i class="grab"></i><h2 style="margin:0 0 8px">Partager une série</h2><input id="sq" type="search" placeholder="Rechercher un animé…" autocomplete="off"><div id="sres"></div></div></div>
<script>(function(){
var CID={{ c.id }},csrf="{{ session.get('csrf','') }}",feed=$('msgs'),last={{ last }},first={{ first }},busy=false,pend=null,rec=null,VMAX={{ MEDIA_MAX.video }},rid=0,ser=null,NAMES={{ names|tojson }};
function mo(n){return n<1e6?Math.max(1,Math.round(n/1000))+' Ko':(n/1e6).toFixed(1).replace('.0','')+' Mo'}
function near(){return window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-180}
function down(sm){window.scrollTo({top:document.documentElement.scrollHeight,behavior:sm?'smooth':'auto'})}
function pad(){document.body.style.paddingBottom=($('cbar').offsetHeight+14)+'px'}
function err(m){var e=$('cerr');e.textContent=m;e.hidden=false;clearTimeout(err.t);err.t=setTimeout(function(){e.hidden=true},6000);pad()}
function put(html,top){var t=document.createElement('div');t.innerHTML=html;var l=[].slice.call(t.children);
 if(top){l.reverse().forEach(function(n){feed.insertBefore(n,feed.firstChild)})}else l.forEach(function(n){n.classList.add('pop');feed.appendChild(n)})}
function poll(){if(document.hidden||busy)return;fetch('/api/groupe/'+CID+'/fil?apres='+last,{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
 if(!d.n||d.last<=last)return;var st=near();$('empty').hidden=true;put(d.html);last=d.last;if(st)down(true)}).catch(function(){})}
setInterval(poll,5000);document.addEventListener('visibilitychange',function(){if(!document.hidden)poll()});
function xhr(u,body,type,onp){return new Promise(function(res,rej){var x=new XMLHttpRequest();x.open('POST',u);x.setRequestHeader('Content-Type',type);x.setRequestHeader('X-CSRF',csrf);
 if(x.upload&&onp)x.upload.onprogress=function(e){if(e.lengthComputable)onp(e.loaded/e.total)};
 x.onload=function(){var d={};try{d=JSON.parse(x.responseText)}catch(e){}if(x.status>=200&&x.status<300&&d.ok)res(d);else rej(new Error(d.error||(x.status==413?'Fichier trop lourd.':'Envoi impossible ('+x.status+').')))};
 x.onerror=function(){rej(new Error('Connexion impossible.'))};x.send(body)})}
function lock(v){busy=v;$('csend').disabled=v}
function prog(f){var p=$('pg');if(p)p.style.width=Math.round(f*100)+'%'}
function clr(){if(pend&&pend.url)URL.revokeObjectURL(pend.url);pend=null;ser=null;$('tray').hidden=true;$('tray').innerHTML='';$('cbody').placeholder='Écris au groupe…';pad()}
function setPend(p){clr();pend=p;p.url=URL.createObjectURL(p.blob);var el=$('tray'),lb={image:'Photo',video:'Vidéo',audio:'Message vocal'}[p.kind]+' · '+mo(p.blob.size);
 el.innerHTML='<div class="tp">'+(p.kind=='image'?'<img src="'+p.url+'" alt="">':p.kind=='video'?'<video src="'+p.url+'#t=0.1" muted playsinline></video>':'<audio src="'+p.url+'" controls></audio>')+'</div><div class="grow mut" style="font-size:.8rem">'+lb+'</div><button type="button" class="ib" id="tx" aria-label="Retirer">✕</button><div class="pbar"><i id="pg"></i></div>';
 el.hidden=false;$('tx').onclick=clr;$('cbody').placeholder='Légende…';pad();down(false);if(p.kind!='audio')$('cbody').focus()}
function done(){lock(false);clr();unreply();var b=$('cbody');b.value='';b.style.height='42px';poll();setTimeout(function(){down(true)},250)}
function fail(e){lock(false);prog(0);err(e.message)}
function send(){if(busy)return;var t=$('cbody').value.trim(),r=rid?'&r='+rid:'',cap=t?'&t='+encodeURIComponent(t.slice(0,300)):'',B='/api/groupe/'+CID+'/envoi?';
 if(pend){lock(true);prog(0);return xhr(B+'k='+pend.kind+cap+r,pend.blob,pend.mime,prog).then(done,fail)}
 if(ser){lock(true);return xhr(B+'k=serie&s='+ser.id+'&st='+encodeURIComponent(ser.title)+'&si='+encodeURIComponent(ser.img||'')+cap+r,'','text/plain').then(done,fail)}
 if(!t)return;lock(true);xhr(B+'k=texte'+r,t,'text/plain;charset=utf-8').then(done,fail)}
function unreply(){rid=0;$('rcl').hidden=true;$('rcl').innerHTML='';pad()}
window.replyTo=function(id){var m=$('m'+id);if(!m)return;rid=id;var el=$('rcl');el.innerHTML='<div class="grow" style="min-width:0;border-left:3px solid var(--or2);padding-left:8px"><b style="color:var(--or2);font-size:.78rem">Réponse à '+esc(m.dataset.who)+'</b><div class="mut" style="font-size:.82rem;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+esc(m.dataset.snip)+'</div></div><button type="button" class="ib" id="rcx" aria-label="Annuler la réponse">✕</button>';el.hidden=false;$('rcx').onclick=unreply;$('cbody').focus();pad()};
function setSerie(s){clr();ser=s;var el=$('tray');el.innerHTML='<div class="tp"><img src="'+esc(s.img)+'" alt=""></div><div class="grow" style="min-width:0"><b>'+esc(s.title)+'</b><div class="mut" style="font-size:.78rem">Série à partager</div></div><button type="button" class="ib" id="tx" aria-label="Retirer">✕</button>';el.hidden=false;$('tx').onclick=clr;$('cbody').placeholder='Ajouter un mot…';pad();down(false);$('cbody').focus()}
var sq=$('sq'),stm;sq.oninput=function(){clearTimeout(stm);var v=this.value.trim();if(v.length<2){$('sres').innerHTML='';return}
 stm=setTimeout(function(){$('sres').innerHTML='<p class="mut" style="text-align:center">Recherche…</p>';L('search',{q:v}).then(function(l){if(sq.value.trim()!=v)return;
  $('sres').innerHTML=l.length?l.slice(0,10).map(function(m,i){return '<button type="button" class="rres" data-i="'+i+'"><img src="'+esc(m.img)+'" alt=""><b>'+esc(m.title)+'</b></button>'}).join(''):'<p class="mut" style="text-align:center">Aucun résultat.</p>';
  [].forEach.call($('sres').querySelectorAll('.rres'),function(b){b.onclick=function(){var m=l[+b.dataset.i];setSerie({id:m.id,title:m.title,img:m.img});closeSheet($('ssheet'))}})},
  function(e){$('sres').innerHTML='<p class="mut" style="text-align:center">Recherche impossible ('+esc(e.message)+').</p>'})},400)};
var sg=$('sug');
function sugg(){var t=$('cbody'),m=/(^|\s)@([\w.\-]{0,20})$/.exec(t.value.slice(0,t.selectionStart));if(!m){sg.hidden=true;return}
 var q=m[2].toLowerCase(),l=NAMES.filter(function(n){return n.toLowerCase().indexOf(q)==0}).slice(0,6);if(!l.length){sg.hidden=true;return}
 sg.innerHTML=l.map(function(n){return '<button type="button">@'+esc(n)+'</button>'}).join('');sg.hidden=false;
 [].forEach.call(sg.children,function(b,i){b.onclick=function(){var p=t.selectionStart,pre=t.value.slice(0,p).replace(/@[\w.\-]{0,20}$/,'@'+l[i]+' ');t.value=pre+t.value.slice(p);t.focus();t.setSelectionRange(pre.length,pre.length);sg.hidden=true;pad()}})}
$('plus').onclick=function(){$('att').hidden=!$('att').hidden;pad()};
$('cf').addEventListener('submit',function(e){e.preventDefault();send()});
$('cbody').addEventListener('input',function(){this.style.height='42px';this.style.height=Math.min(this.scrollHeight,120)+'px';sugg();pad()});
$('cbody').addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey&&!matchMedia('(pointer:coarse)').matches){e.preventDefault();send()}});
function fit(file,max,lim){return new Promise(function(res,rej){var u=URL.createObjectURL(file),im=new Image();im.onerror=function(){URL.revokeObjectURL(u);rej(new Error('Image illisible.'))};
 im.onload=function(){URL.revokeObjectURL(u);var r=Math.min(1,max/Math.max(im.width,im.height)),w=Math.round(im.width*r),h=Math.round(im.height*r),c=document.createElement('canvas');c.width=w;c.height=h;var x=c.getContext('2d');x.fillStyle='#000';x.fillRect(0,0,w,h);x.drawImage(im,0,0,w,h);
  (function t(q){c.toBlob(function(b){if(!b)return rej(new Error('Conversion impossible.'));if(b.size>lim&&q>.4)return t(q-.1);b.size>lim?rej(new Error('Image trop lourde.')):res(b)},'image/jpeg',q)})(.85)};im.src=u})}
$('fimg').onchange=function(){var f=this.files[0];this.value='';$('att').hidden=true;if(!f)return;
 if(f.type==='image/gif'&&f.size<={{ MEDIA_MAX.gif }})return setPend({kind:'image',blob:f,mime:'image/gif'});
 fit(f,1280,560000).then(function(b){setPend({kind:'image',blob:b,mime:'image/jpeg'})},function(e){err(e.message)})};
$('fvid').onchange=function(){var f=this.files[0];this.value='';$('att').hidden=true;if(!f)return;
 if(f.size>VMAX)return err('Vidéo trop lourde ('+mo(f.size)+'). Maximum '+mo(VMAX)+' : choisis un clip plus court.');
 setPend({kind:'video',blob:f,mime:f.type||'video/mp4'})};
if(window.MediaRecorder&&navigator.mediaDevices&&navigator.mediaDevices.getUserMedia)$('mic').hidden=false;
$('mic').onclick=function(){navigator.mediaDevices.getUserMedia({audio:true}).then(function(st){
 var mt=['audio/webm;codecs=opus','audio/mp4','audio/webm','audio/ogg;codecs=opus'].filter(function(t){return MediaRecorder.isTypeSupported(t)})[0],ch=[];
 var r=rec={mr:new MediaRecorder(st,mt?{mimeType:mt}:{}),t0:Date.now(),cancel:false};
 r.mr.ondataavailable=function(e){if(e.data&&e.data.size)ch.push(e.data)};
 r.mr.onstop=function(){st.getTracks().forEach(function(t){t.stop()});clearInterval(r.iv);rec=null;$('rec').hidden=true;$('cf').hidden=false;pad();
  if(r.cancel||!ch.length||Date.now()-r.t0<800)return;var b=new Blob(ch,{type:r.mr.mimeType||mt||'audio/webm'});pend={kind:'audio',blob:b,mime:b.type};send()};
 r.mr.start();$('rec').hidden=false;$('cf').hidden=true;pad();
 r.iv=setInterval(function(){var s=Math.floor((Date.now()-r.t0)/1000);$('rect').textContent=Math.floor(s/60)+':'+('0'+s%60).slice(-2);if(s>=120)r.mr.stop()},250)},
 function(){err("Micro refusé : autorise-le dans les réglages du navigateur.")})};
$('recok').onclick=function(){if(rec)rec.mr.stop()};$('recx').onclick=function(){if(rec){rec.cancel=true;rec.mr.stop()}};
window.react=function(pid,e){xhr('/api/groupe/reagir/'+pid,JSON.stringify({e:e}),'application/json').then(function(d){var b=$('rx'+pid);b.innerHTML=d.html;var c=b.querySelector('.rch.on');if(c)c.classList.add('bump')},function(x){err(x.message)})};
window.zoom=function(src){var o=document.createElement('div');o.className='zoom';o.innerHTML='<img src="'+src+'" alt="">';o.onclick=function(){o.remove()};document.body.appendChild(o)};
var ol=$('older');if(ol)ol.onclick=function(){var b=this;fetch('/api/groupe/'+CID+'/fil?avant='+first,{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
 var h=document.documentElement,old=h.scrollHeight;put(d.html,true);if(d.first)first=d.first;window.scrollTo(0,window.scrollY+h.scrollHeight-old);if(!d.more)b.parentNode.remove()}).catch(function(){err('Chargement impossible.')})};
pad();addEventListener('resize',pad);var hm=/^#m(\d+)$/.exec(location.hash);if(!(hm&&window.goto(+hm[1])===false))down(false);
})()</script>{% endif %}
{% if can_edit %}<div class="sheet" id="esheet" hidden onclick="if(event.target===this)closeSheet(this)"><div class="shp"><i class="grab"></i><h2 style="margin:0 0 10px">Personnaliser le groupe</h2>
<div class="row" style="flex-wrap:wrap;margin-bottom:6px"><button type="button" class="btn s" onclick="$('gfa').click()">{{ ic('image',16) }} Photo du groupe</button><button type="button" class="btn s g" onclick="$('gfc').click()">{{ ic('image',16) }} Bannière</button></div>
<input type="file" id="gfa" accept="image/*" hidden><input type="file" id="gfc" accept="image/*" hidden><small id="gum" class="mut" style="display:block;min-height:1.2em">Les images sont recadrées et réduites automatiquement.</small>
{% if c.av or c.cv %}<div class="row" style="flex-wrap:wrap;margin:6px 0">{% if c.av %}<form method="post" action="/groupe/{{ c.id }}/photo/supprimer">{{ csrf() }}<input type="hidden" name="kind" value="avatar"><button class="btn s g" onclick="return confirm('Retirer la photo ?')">Retirer la photo</button></form>{% endif %}{% if c.cv %}<form method="post" action="/groupe/{{ c.id }}/photo/supprimer">{{ csrf() }}<input type="hidden" name="kind" value="cover"><button class="btn s g" onclick="return confirm('Retirer la bannière ?')">Retirer la bannière</button></form>{% endif %}</div>{% endif %}
<form method="post">{{ csrf() }}<input type="hidden" name="action" value="edit"><label>Thème</label><input name="topic" maxlength="40" value="{{ c.topic }}"><label>Description</label><textarea name="descr" maxlength="300" style="min-height:84px">{{ c.descr }}</textarea><p><button class="btn w">Enregistrer</button></p></form></div></div>
<script>(function(){function gup(k,f){if(!f)return;$('gum').textContent='Envoi en cours…';var av=k=='avatar';crop(f,av?256:960,av?256:360,av?68000:165000).then(function(b){return fetch('/api/groupe/{{ c.id }}/img/'+k,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'image/jpeg','X-CSRF':CSRF},body:b})}).then(function(r){return r.json()}).then(function(d){if(d.error)throw new Error(d.error);location.reload()}).catch(function(e){$('gum').textContent='⚠️ '+e.message})}
$('gfa').onchange=function(){var f=this.files[0];this.value='';gup('avatar',f)};$('gfc').onchange=function(){var f=this.files[0];this.value='';gup('cover',f)}})()</script>{% endif %}{% endblock %}"""

T["quiz.html"] = r"""{% extends "base.html" %}{% from "m.html" import cd with context %}{% block c %}
<div id="home"><h1>Tournoi</h1><div class="card" style="text-align:center"><small class="mut">Début dans :</small>{{ cd(eta) }}
<div class="kpi" style="margin:8px 0"><div><b>{{ stats.n if stats else 0 }}</b><small>Tentatives</small></div><div><b>{{ QC }}</b><small>Questions</small></div><div><b>{{ QT }} s</b><small>Temps</small></div></div>
{% if me %}<button class="btn w" onclick="play('tournoi')" {{ 'disabled style=opacity:.5' if played }}>{{ 'Déjà joué aujourd\'hui' if played else 'Participer au tournoi' }}</button><p style="margin:8px 0 0"><button class="btn g w" onclick="play('solo')">Entraînement solo</button></p>
{% else %}<a class="btn w" href="/connexion?next=/quiz">Connecte-toi pour jouer</a>{% endif %}<p id="msg" class="mut"></p></div>
{% if stats %}<h2>Mes stats</h2><div class="kpi"><div><b>{{ stats.b }}/{{ QC }}</b><small>Meilleur score</small></div><div><b>{{ stats.g }}</b><small>Bonnes réponses</small></div><div><b>{{ (100*stats.g//stats.t) if stats.t else 0 }}%</b><small>Réussite</small></div></div>{% endif %}
<h2>Classement du jour</h2><div class="grp">{% for b in board %}<a class="it" href="/profil/{{ b.username }}"><b style="color:var(--or2);width:22px">{{ loop.index }}</b>{{ ava(b.username,30) }}<span class="grow">{{ b.username }}</span><b>{{ b.g }}/{{ QC }}</b></a>{% else %}<div class="it mut">Personne n'a encore joué aujourd'hui.</div>{% endfor %}</div>
<p class="mut" style="font-size:.72rem;text-align:center">Questions générées en direct depuis Jikan / MyAnimeList</p></div>
<div id="game" hidden><div class="row"><b id="pg">1/10</b><span class="grow"></span><b id="tm" style="color:var(--or2)">15</b></div><div class="qbar"><i id="bar"></i></div><div class="card"><p id="qt" style="font-weight:700;margin:0"></p><img id="qi" class="qimg" alt="" hidden><div id="op"></div></div></div>
<div id="end" hidden class="card" style="text-align:center"><h1 id="sc"></h1><p class="mut">Bien joué !</p><a class="btn w" href="/quiz">Retour</a></div>
<script>
var Q,i,good,t0,timer,left,mode,T={{ QT }},csrf="{{ session.get('csrf','') }}";
function mask(t,title){(title.match(/\w{4,}/g)||[]).forEach(function(w){t=t.replace(new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi'),'▮▮▮')});return t}
function build(){return L('top',{page:1+Math.floor(Math.random()*3)}).then(function(l){var pool=shuf(l.slice());if(pool.length<8)throw new Error('données insuffisantes');var qs=[];
 pool.slice(0,10).forEach(function(it){var o=shuf(pool.filter(function(x){return x.id!==it.id})).slice(0,3).map(function(x){return x.title}),k=Math.floor(Math.random()*3);
  if(k==0&&it.year){var ys={};ys[it.year]=1;while(Object.keys(ys).length<4)ys[it.year+[-4,-3,-2,-1,1,2,3][Math.floor(Math.random()*7)]]=1;var op=Object.keys(ys).sort();qs.push({t:'En quelle année est sorti « '+it.title+' » ?',img:it.img,o:op,a:op.indexOf(String(it.year))});return}
  var op=shuf([it.title].concat(o)),a=op.indexOf(it.title);
  if(k==1&&it.syn.length>80)qs.push({t:'Quel animé se cache derrière ce résumé ? « '+mask(it.syn.slice(0,230),it.title)+'… »',img:'',o:op,a:a});else qs.push({t:'Quel est cet animé ?',img:it.img,o:op,a:a})});return qs})}
function play(m){mode=m;$('msg').textContent='Chargement des questions…';build().then(function(q){Q=q;i=0;good=0;t0=Date.now();$('home').hidden=true;$('game').hidden=false;show()}).catch(function(e){$('msg').textContent='Source injoignable ('+e.message+'). Réessaie.'})}
function show(){var q=Q[i];$('pg').textContent=(i+1)+'/'+Q.length;$('qt').textContent=q.t;var im=$('qi');im.hidden=!q.img;if(q.img)im.src=q.img;$('op').innerHTML='';
 q.o.forEach(function(o,k){var b=document.createElement('button');b.className='opt';b.textContent=o;b.onclick=function(){pick(k)};$('op').appendChild(b)});left=T;tick();clearInterval(timer);timer=setInterval(function(){left--;tick();if(left<=0)pick(-1)},1000)}
function tick(){$('tm').textContent=left;$('bar').style.width=(100*left/T)+'%'}
function pick(k){clearInterval(timer);var a=Q[i].a;if(k==a)good++;document.querySelectorAll('.opt').forEach(function(b,n){b.disabled=true;if(n==a)b.classList.add('ok');else if(n==k)b.classList.add('no')});setTimeout(next,900)}
function next(){i++;if(i<Q.length)return show();$('game').hidden=true;$('end').hidden=false;$('sc').textContent=good+' / '+Q.length;
 fetch('/api/quiz/finish',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF':csrf},body:JSON.stringify({mode:mode,good:good,total:Q.length,ms:Date.now()-t0})})}
</script>{% endblock %}"""

T["profile.html"] = r"""{% extends "base.html" %}{% from "m.html" import post with context %}{% block c %}
{% set bk = u.p_banner if (u.p_banner!='img' or u.bv) else 'sunset' %}
<div class="pcard" style="--c:{{ u.p_color or '#ff6a1a' }}"><div class="pbanner bn-{{ bk }}" {% if bk=='img' %}style="background-image:url(/u/{{ u.username }}/banner?v={{ u.bv }})"{% endif %}><div class="fxbox" id="fxb" data-fx="{{ u.p_fx or 'none' }}"></div></div>
<div class="pmeta">{{ ava(u.username,92) }}<br><h1 class="nm nm-{{ u.p_name or 'normal' }}">{{ u.username }}</h1>{% if u.otype and not locked %}<br><span class="otype">{{ u.otype }}</span>{% endif %}{% if u.p_status and not locked %}<br><span class="pstatus">{{ u.p_status }}</span>{% endif %}<br><small class="mut">Membre depuis {{ u.created[:10] }}</small>{% if not locked %}<p>{{ u.bio }}</p>{% endif %}
{% if me and not own %}<a class="btn s" href="/messages/{{ u.username }}">{{ ic('msg',16) }} Message</a> <form method="post" action="/bloquer/{{ u.username }}" style="display:inline">{{ csrf() }}<button class="btn s g">Bloquer</button></form> <form method="post" action="/signaler/user/{{ u.id }}" style="display:inline">{{ csrf() }}<input type="hidden" name="reason" value="Contenu inapproprié"><button class="btn s g">Signaler</button></form>{% elif own %}<a class="btn s" href="/profil/modifier">✨ Personnaliser</a> <a class="btn s g" href="/parametres">{{ ic('sliders',16) }} Paramètres</a>{% endif %}</div></div>
{% if locked %}<div class="card" style="text-align:center;margin-top:14px">{{ ic('lock') }} Ce profil est privé.</div>{% else %}
<div class="kpi" style="margin:14px 0 8px"><div><b>{{ stt.np }}</b><small>Posts</small></div><div><b>{{ favs|length }}</b><small>Favoris</small></div><div><b>{{ st.b }}/10</b><small>Meilleur quiz</small></div></div>
<div class="kpi" style="margin:0 0 14px"><div><b>{{ stt.lk }}</b><small>Likes reçus</small></div><div><b>{{ stt.ng }}</b><small>Groupes</small></div><div><b>{{ stt.nq }}</b><small>Quiz joués</small></div></div>
<h2>Badges <small class="mut" style="font-weight:400">{{ badges|selectattr('ok')|list|length }}/{{ badges|length }}</small></h2><div class="badges">{% for b in badges %}<div class="bdg{{ ' on' if b.ok }}" title="{{ b.d }}"><span>{{ b.e }}</span><small>{{ b.n }}</small></div>{% endfor %}</div>
{% if favs %}<h2>Séries favorites</h2><div class="stories">{% for f in favs %}<a class="st" style="width:90px" href="/serie/{{ f.mal_id }}{{ '?k=manga' if f.kind=='manga' }}"><img src="{{ f.img }}" alt="" style="border-radius:14px;aspect-ratio:2/3;border:0"><span>{{ f.title }}</span></a>{% endfor %}</div>{% endif %}
<h2>Publications</h2>{% for p in posts %}{{ post(p) }}{% else %}<p class="mut">Rien pour l'instant.</p>{% endfor %}{% endif %}
<script>fx($('fxb'),$('fxb').dataset.fx)</script>{% endblock %}"""

T["inbox.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Messages</h1><div class="grp">{% for r in rows %}<a class="it" href="/messages/{{ r.username }}">{{ ava(r.username,40) }}<span class="grow"><b>{{ r.username }}</b><small>{{ r.body[:50] }} · {{ ago(r.created) }}</small></span>{% if r.un %}<b style="color:var(--or)">{{ r.un }}</b>{% endif %}</a>
{% else %}<div class="it mut">Aucune conversation. Va sur un profil pour écrire.</div>{% endfor %}</div>{% endblock %}"""

T["thread.html"] = r"""{% extends "base.html" %}{% set back='/messages' %}{% block c %}<h1>{{ o.username }}</h1>{% for m in ms %}<div class="bubble {{ 'me' if m.s==me.id }}">{{ m.body }}</div>{% endfor %}
<form method="post" class="row" style="margin-top:12px">{{ csrf() }}<input name="body" maxlength="1000" placeholder="Message…" required autofocus><button class="btn">{{ ic('send',18) }}</button></form>{% endblock %}"""

T["notifs.html"] = r"""{% extends "base.html" %}{% block c %}<div class="row"><h1 class="grow">Notifications</h1><a href="/messages">{{ ic('msg') }}</a></div>
<form method="post" class="row">{{ csrf() }}<button class="btn s g" name="a" value="read">Tout lire</button><button class="btn s g" name="a" value="clear">Vider</button></form>
<div class="grp" style="margin-top:12px">{% for n in rows %}<a class="it" href="{{ n.link }}"><span class="ico">{{ ic('bell',18) }}</span><span class="grow" style="{{ '' if n.read else 'font-weight:700' }}">{{ n.text }}<small>{{ ago(n.created) }}</small></span></a>{% else %}<div class="it mut">Rien de neuf.</div>{% endfor %}</div>{% endblock %}"""

T["settings.html"] = r"""{% extends "base.html" %}{% from "m.html" import item, sw with context %}{% block c %}<h1>Paramètres</h1>
<div class="card row">{{ ava(me.username,56) }}<div class="grow"><b style="font-size:1.1rem">{{ me.username }}</b><small class="mut" style="display:block">Membre depuis {{ me.created[:10] }}</small></div><a class="btn s g" href="/profil/{{ me.username }}">Voir</a></div>
<form method="post">{{ csrf() }}<input type="hidden" name="action" value="prefs">
<div class="sec">Profil</div><div class="grp" style="padding:12px 16px"><label style="margin-top:0">Biographie</label><textarea name="bio" maxlength="300">{{ me.bio }}</textarea><label>E-mail (pour te connecter et récupérer ton compte)</label><input type="email" name="email" maxlength="120" value="{{ me.email or '' }}" placeholder="facultatif"></div>
<div class="sec">Apparence</div><div class="grp">{{ sw('theme', me.theme=='light', 'Thème clair', 'Désactivé = thème sombre', 'compass') }}</div>
<div class="sec">Notifications</div><div class="grp">{{ sw('notify', me.notify, 'Notifications', "J'aime, commentaires", 'bell') }}</div>
<div class="sec">Confidentialité</div><div class="grp">{{ sw('privacy', me.privacy=='private', 'Profil privé', 'Seul toi vois ton profil', 'lock') }}{{ sw('dm', me.dm=='all', 'Messages privés', 'Autoriser les messages', 'msg') }}</div>
<p><button class="btn w">Enregistrer</button></p></form>
<div class="sec">Messages</div><div class="grp">{{ item('msg','Mes messages','','/messages') }}{{ item('star','Ma liste de suivi','À voir · En cours · Terminé','/liste') }}{{ item('user','Personnaliser mon profil','Photo, bannière, animations','/profil/modifier') }}{{ item('lock','Utilisateurs bloqués','','/bloques') }}</div>
<div class="sec">Sécurité</div><div class="grp" style="padding:12px 16px"><form method="post">{{ csrf() }}<input type="hidden" name="action" value="password"><label style="margin-top:0">Mot de passe actuel</label><input type="password" name="old" autocomplete="current-password"><label>Nouveau (8+)</label><input type="password" name="new" autocomplete="new-password"><p><button class="btn s">Changer</button></p></form>
<form method="post">{{ csrf() }}<input type="hidden" name="action" value="regen"><label>Code de récupération {{ '(défini)' if me.recovery else '(aucun)' }}</label><input type="password" name="password" placeholder="Ton mot de passe pour générer un nouveau code" autocomplete="current-password"><p><button class="btn s g">Générer un nouveau code</button></p></form>
<form method="post">{{ csrf() }}<input type="hidden" name="action" value="logout_all"><button class="btn s g">Déconnecter les autres appareils</button></form></div>
<div class="sec">Légal</div><div class="grp">{{ item('lock','Politique de confidentialité','Tes données et tes droits','/confidentialite') }}{{ item('shield',"Conditions d'utilisation",'Les règles de la communauté','/conditions') }}</div>
<div class="sec">Contact & aide</div><div class="grp">{{ item('mail','E-mail',email,'mailto:'+email) }}{{ item('msg','WhatsApp',wa_show,'https://wa.me/'+wa) }}{{ item('users','Facebook','',fb) }}{{ item('info','À propos de SenpaiX','','/a-propos') }}</div>
<div class="sec">Mes données</div><div class="grp"><form method="post" action="/export">{{ csrf() }}<button class="it"><span class="ico">{{ ic('dl',19) }}</span><span class="grow">Exporter mes données (JSON)</span></button></form></div>
<div class="grp"><form method="post" action="/deconnexion" onsubmit="try{caches.keys().then(function(k){k.forEach(function(x){caches.delete(x)})})}catch(e){}">{{ csrf() }}<button class="it"><span class="ico">{{ ic('out',19) }}</span><span class="grow">Se déconnecter</span></button></form></div>
<div class="sec" style="color:#ff6b81">Zone dangereuse</div><div class="grp" style="padding:12px 16px"><form method="post" onsubmit="return confirm('Supprimer définitivement ?')">{{ csrf() }}<input type="hidden" name="action" value="delete"><small class="mut">Irréversible. Tape ton pseudo et ton mot de passe.</small><input name="confirm" placeholder="Ton pseudo" style="margin:8px 0"><input type="password" name="password" placeholder="Mot de passe"><p><button class="btn s r">Supprimer mon compte</button></p></form></div>
<p class="mut" style="text-align:center;font-size:.72rem">SenpaiX v4 · Jikan + AniList</p>{% endblock %}"""

T["auth.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><div class="logo" style="font-size:2.6rem">Senpai<b>X</b></div><p class="mut" style="margin:4px 0 14px">L'univers Otaku autrement.</p>
<div class="seg"><a class="{{ '' if reg else 'on' }}" href="/connexion">Connexion</a><a class="{{ 'on' if reg }}" href="/inscription">Inscription</a></div>
{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ c }}" style="text-align:left">{{ m }}</div>{% endfor %}
<form method="post" class="card">{{ csrf() }}
{% if reg %}<label>Pseudo</label><input name="username" id="u" required minlength="3" maxlength="20" autocomplete="username" autocapitalize="none" value="{{ v.get('username','') }}"><small id="um" class="hint"></small>
<label>E-mail <span style="font-weight:400">(facultatif — te permet de te connecter avec)</span></label><input type="email" name="email" maxlength="120" autocomplete="email" value="{{ v.get('email','') }}">
<label>Mot de passe</label><div class="pw"><input type="password" name="password" id="p" required minlength="8" maxlength="128" autocomplete="new-password"><button type="button" class="eye" onclick="eye('p',this)">Voir</button></div><div class="meter"><i id="mt"></i></div><small id="pm" class="hint">8 caractères minimum</small>
<label>Confirmer le mot de passe</label><input type="password" name="password2" id="p2" required autocomplete="new-password"><small id="p2m" class="hint"></small>
<label class="chk"><input type="checkbox" name="age" required> J'ai 13 ans ou plus</label>
<label class="chk"><input type="checkbox" name="cgu" required> <span>J'accepte les <a href="/conditions" target="_blank" style="color:var(--or)">conditions d'utilisation</a> et la <a href="/confidentialite" target="_blank" style="color:var(--or)">politique de confidentialité</a></span></label>
<p><button class="btn w">Créer mon compte</button></p>
{% else %}<label>Pseudo ou e-mail</label><input name="username" required autocomplete="username" autocapitalize="none" value="{{ v.get('username','') }}">
<label>Mot de passe</label><div class="pw"><input type="password" name="password" id="p" required autocomplete="current-password"><button type="button" class="eye" onclick="eye('p',this)">Voir</button></div>
<div class="row" style="margin-top:6px"><label class="chk grow" style="margin:6px 0"><input type="checkbox" name="remember" checked> Rester connecté</label><a href="/mot-de-passe-oublie" style="color:var(--or);font-size:.82rem">Mot de passe oublié ?</a></div>
<p><button class="btn w">Se connecter</button></p>{% endif %}</form>
<p class="mut" style="font-size:.85rem">{% if reg %}Déjà un compte ? <a href="/connexion" style="color:var(--or)">Connexion</a>{% else %}Pas encore de compte ? <a href="/inscription" style="color:var(--or)">Inscription</a>{% endif %} · <a href="/">Explorer sans compte</a></p>
<p class="mut" style="font-size:.72rem"><a href="/conditions">Conditions</a> · <a href="/confidentialite">Confidentialité</a></p></div>
<script>function eye(i,b){var e=$(i);e.type=e.type=='password'?'text':'password';b.textContent=e.type=='password'?'Voir':'Cacher'}
if($('u')){var tm;$('u').oninput=function(){clearTimeout(tm);var v=this.value.trim(),o=$('um');if(v.length<3){o.textContent='';return}tm=setTimeout(function(){fetch('/api/pseudo?u='+encodeURIComponent(v)).then(function(r){return r.json()}).then(function(d){o.textContent=(d.ok?'✅ ':'❌ ')+d.msg;o.style.color=d.ok?'#4cd18a':'#ff7b8d'}).catch(function(){})},350)};
$('p').oninput=function(){var v=this.value,s=0;if(v.length>=8)s++;if(v.length>=12)s++;if(/[A-Z]/.test(v)&&/[a-z]/.test(v))s++;if(/\d/.test(v)&&/[^\w]/.test(v))s++;var m=$('mt');m.style.width=(s*25)+'%';m.style.background=['#e5233b','#ff6a1a','#ffc233','#4cd18a','#4cd18a'][s];$('pm').textContent=v.length<8?'8 caractères minimum':['Faible','Moyen','Bon','Excellent'][Math.max(0,s-1)]};
$('p2').oninput=function(){var o=$('p2m');if(!this.value){o.textContent='';return}var ok=this.value==$('p').value;o.textContent=ok?'✅ Identiques':'❌ Différents';o.style.color=ok?'#4cd18a':'#ff7b8d'}}</script>{% endblock %}"""

T["about.html"] = r"""{% extends "base.html" %}{% block c %}<h1>À propos</h1><div class="card"><div class="row">{{ ava(creator,56) }}<div><b>{{ creator }}</b><small class="mut" style="display:block">Créateur · Guinée 🇬🇳</small></div></div><p>{{ bio }}</p></div>
<div class="grp"><a class="it" href="mailto:{{ email }}"><span class="ico">{{ ic('mail',19) }}</span><span class="grow">{{ email }}</span></a><a class="it" href="https://wa.me/{{ wa }}"><span class="ico">{{ ic('msg',19) }}</span><span class="grow">WhatsApp {{ wa_show }}</span></a><a class="it" href="{{ fb }}"><span class="ico">{{ ic('users',19) }}</span><span class="grow">Facebook</span></a></div>
<p class="mut" style="font-size:.8rem">Sources des données : Jikan (MyAnimeList) et AniList. Aucune publicité.</p>{% endblock %}"""

T["diag.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Diagnostic</h1><p class="mut">Test des sources depuis ton navigateur.</p>
<div class="grp"><div class="it"><span class="grow">Internet<small id="d0">…</small></span></div><div class="it"><span class="grow">AniList<small id="d1">…</small></span></div><div class="it"><span class="grow">Jikan (MyAnimeList)<small id="d2">…</small></span></div></div>
<p><button class="btn w" onclick="location.reload()">Relancer le test</button></p>
<script>function t(id,f){var a=Date.now();f().then(function(){$(id).textContent='✅ OK ('+(Date.now()-a)+' ms)'}).catch(function(e){$(id).textContent='❌ '+e.message})}
$('d0').textContent=navigator.onLine?'✅ en ligne':'❌ hors ligne';
t('d1',function(){return AL('{Page(perPage:1){media{id}}}')});t('d2',function(){return get(JK+'/seasons/now?limit=1')});</script>{% endblock %}"""

T["welcome.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><h1 style="font-size:2rem">Bienvenue ! 🎉</h1><p class="mut">Voici ton <b>code de récupération</b>. Il te permet de retrouver ton compte si tu oublies ton mot de passe. Il ne sera <b>plus jamais affiché</b> : note-le ou fais une capture d'écran.</p>
<div class="card" style="text-align:center"><div id="rc" style="font:800 1.7rem monospace;letter-spacing:2px;color:var(--or2);word-break:break-all">{{ code }}</div><p><button class="btn s g" onclick="navigator.clipboard&&navigator.clipboard.writeText('{{ code }}');this.textContent='Copié ✔'">Copier</button></p></div>
<a class="btn w" href="/">J'ai noté mon code — continuer</a></div>{% endblock %}"""

T["forgot.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><h1>Mot de passe oublié</h1><p class="mut">Entre ton pseudo (ou e-mail) et le code de récupération reçu à l'inscription.</p>
{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ c }}" style="text-align:left">{{ m }}</div>{% endfor %}
<form method="post" class="card">{{ csrf() }}<label>Pseudo ou e-mail</label><input name="username" required value="{{ v.get('username','') }}" autocapitalize="none"><label>Code de récupération</label><input name="code" required placeholder="XXXX-XXXX-XXXX" autocapitalize="characters" autocomplete="off">
<label>Nouveau mot de passe</label><input type="password" name="new" required minlength="8" autocomplete="new-password"><label>Confirmer</label><input type="password" name="new2" required minlength="8" autocomplete="new-password"><p><button class="btn w">Réinitialiser</button></p></form>
<p class="mut" style="font-size:.82rem">Plus de code ? Écris-nous : <a href="mailto:{{ email }}" style="color:var(--or)">{{ email }}</a></p><a class="mut" href="/connexion">← Connexion</a></div>{% endblock %}"""

T["priv.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Politique de confidentialité</h1><p class="mut">Dernière mise à jour : 24 octobre 2026</p>
<div class="card"><h2>Qui sommes-nous ?</h2><p>SenpaiX est un réseau social pour fans de mangas et d'animés, créé et géré par {{ creator }} (Guinée). Contact : <a href="mailto:{{ email }}" style="color:var(--or)">{{ email }}</a>.</p>
<h2>Données que nous collectons</h2><p>• <b>Compte</b> : pseudo, mot de passe (stocké uniquement sous forme chiffrée/hachée, nous ne pouvons pas le lire), e-mail (facultatif), code de récupération (haché).<br>• <b>Contenu</b> : ta photo de profil et ta bannière (réduites puis stockées dans notre base de données), tes publications, commentaires, messages privés, favoris, groupes rejoints et scores de quiz.<br>• <b>Technique</b> : un cookie de session indispensable à ta connexion. Pas de pistage publicitaire, pas de cookies marketing.</p>
<h2>Pourquoi ?</h2><p>Uniquement pour faire fonctionner l'application : te connecter, afficher ton profil et tes publications, gérer les messages, notifications, classements et favoris.</p>
<h2>Services tiers</h2><p>Les fiches, affiches et tendances viennent de <b>AniList</b> et de <b>Jikan / MyAnimeList</b>. Quand tu les consultes, ton navigateur contacte directement ces services : ils peuvent voir ton adresse IP, selon leurs propres politiques. Nous ne leur envoyons ni ton pseudo ni tes données de compte. Ton navigateur garde aussi un cache local (catalogue) pour aller plus vite.</p>
<p>Les bandes-annonces sont lues via <b>YouTube</b> en mode « sans cookies » (youtube-nocookie.com). Quand tu lances une vidéo, ton navigateur contacte YouTube (Google), qui peut voir ton adresse IP, selon sa propre politique.</p>
<h2>Modération</h2><p>Les modérateurs peuvent consulter les contenus publics (publications, commentaires, messages de groupe) et les signalements pour faire respecter les conditions. Ils ne lisent pas tes messages privés.</p>
<h2>Partage</h2><p>Nous ne vendons ni ne louons tes données et n'affichons aucune publicité. Tes publications sont visibles des autres membres ; un profil « privé » masque ton profil. Les messages privés ne sont visibles que des deux personnes. Nous pouvons divulguer des informations si la loi l'exige.</p>
<h2>Conservation et sécurité</h2><p>Tes données sont conservées tant que ton compte existe. Les mots de passe sont hachés, les formulaires protégés contre les attaques CSRF, les tentatives de connexion limitées. Aucun système n'est infaillible : choisis un mot de passe unique et solide.</p>
<h2>Tes droits</h2><p>Dans <b>Paramètres</b> tu peux : modifier ton profil et ton e-mail, <b>exporter</b> tes données (JSON) et <b>supprimer définitivement</b> ton compte (tout ton contenu est effacé). Pour toute autre demande, écris-nous.</p>
<h2>Mineurs</h2><p>SenpaiX est réservé aux personnes de 13 ans et plus. Si tu penses qu'un enfant plus jeune a un compte, préviens-nous pour qu'il soit supprimé.</p>
<h2>Modifications</h2><p>Nous pouvons mettre à jour cette politique ; la date ci-dessus sera modifiée et les changements importants annoncés dans l'application.</p></div>{% endblock %}"""

T["cgu.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Conditions d'utilisation</h1><p class="mut">Dernière mise à jour : 24 octobre 2026</p>
<div class="card"><h2>1. Acceptation</h2><p>En créant un compte ou en utilisant SenpaiX, tu acceptes ces conditions et la <a href="/confidentialite" style="color:var(--or)">politique de confidentialité</a>. Tu dois avoir au moins 13 ans.</p>
<h2>2. Ton compte</h2><p>Tu es responsable de ton mot de passe et de ton code de récupération. Un pseudo = une personne ; l'usurpation d'identité est interdite. Tu peux supprimer ton compte à tout moment dans Paramètres.</p>
<h2>3. Règles de la communauté</h2><p>Sont interdits : harcèlement, insultes, discours de haine, menaces, contenu sexuel ou violent choquant, spam et arnaques, partage de données personnelles d'autrui, contenu illégal. Pense à avertir en cas de <b>spoiler</b>. Sois respectueux : on est ici pour partager notre passion.</p>
<h2>4. Ton contenu</h2><p>Tu restes propriétaire de ce que tu publies. Tu nous autorises à l'afficher dans l'application. Ne publie que ce que tu as le droit de partager (pas de copie d'œuvres protégées). Les affiches et informations d'œuvres appartiennent à leurs ayants droit et proviennent d'AniList et de MyAnimeList.</p>
<h2>5. Modération</h2><p>Nous pouvons supprimer du contenu ou suspendre un compte qui enfreint ces règles, sans préavis en cas de faute grave.</p>
<h2>6. Quiz et classements</h2><p>Les quiz sont un jeu amical, sans gain financier. Toute triche ou exploitation d'une faille peut entraîner l'exclusion du classement.</p>
<h2>7. Disponibilité et responsabilité</h2><p>Le service est fourni « tel quel », sans garantie de disponibilité continue. Il dépend de services tiers (AniList, Jikan) qui peuvent être indisponibles. Nous ne sommes pas responsables des contenus publiés par les membres, ni des dommages indirects, dans les limites permises par la loi.</p>
<h2>8. Modifications et contact</h2><p>Ces conditions peuvent évoluer ; continuer à utiliser SenpaiX vaut acceptation. Questions : <a href="mailto:{{ email }}" style="color:var(--or)">{{ email }}</a>.</p></div>{% endblock %}"""

T["mylist.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Ma liste</h1>
<div class="pills">{% for k,l in STATUSES.items() %}<a class="pill {{ 'on' if t==k }}" href="/liste?t={{ k }}">{{ l }} · {{ counts.get(k,0) }}</a>{% endfor %}</div>
{% for r in rows %}<div class="card row" style="align-items:flex-start"><a href="/serie/{{ r.mal_id }}{{ '?k=manga' if r.kind=='manga' }}"><img src="{{ r.img }}" alt="" loading="lazy" style="width:64px;aspect-ratio:2/3;object-fit:cover;border-radius:10px"></a>
<div class="grow"><a href="/serie/{{ r.mal_id }}{{ '?k=manga' if r.kind=='manga' }}"><b>{{ r.title }}</b></a>
<small class="mut" style="display:block">{{ 'Ch.' if r.kind=='manga' else 'Ép.' }} {{ r.ep }}{% if r.total %} / {{ r.total }}{% endif %}{% if r.score %} · ★ {{ r.score }}/10{% endif %}</small>
<small class="nx" data-id="{{ r.mal_id }}" style="color:var(--or2);display:block"></small>
<div class="row" style="margin-top:8px">{% for a,l in [('dec','−'),('inc','+1')] %}<form method="post" action="/liste/{{ r.mal_id }}">{{ csrf() }}<input type="hidden" name="action" value="{{ a }}"><button class="btn s g">{{ l }}</button></form>{% endfor %}
<form method="post" action="/liste/{{ r.mal_id }}" onsubmit="return confirm('Retirer ?')">{{ csrf() }}<input type="hidden" name="action" value="remove"><button class="btn s g">{{ ic('trash',14) }}</button></form></div></div></div>
{% else %}<p class="mut" style="text-align:center;margin-top:24px">Rien ici. Ajoute des séries depuis leur fiche.</p>{% endfor %}
{% if ids %}<script>NX({{ ids|tojson }}).then(function(o){document.querySelectorAll('.nx').forEach(function(e){var m=o[e.dataset.id];if(m)e.textContent='📅 Épisode '+m.ep+' dans '+dur(m.at-Date.now()/1000)})}).catch(function(){})</script>{% endif %}{% endblock %}"""

T["blocked.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Utilisateurs bloqués</h1><div class="grp">{% for r in rows %}<div class="it">{{ ava(r.username,34) }}<span class="grow">{{ r.username }}</span><form method="post" action="/bloquer/{{ r.username }}">{{ csrf() }}<button class="btn s g">Débloquer</button></form></div>{% else %}<div class="it mut">Personne n'est bloqué.</div>{% endfor %}</div>{% endblock %}"""

T["adm.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Modération</h1>
<div class="pills">{% for k,l,h in [('reports','Tableau de bord','/admin'),('members','Membres','/admin/membres'),('content','Contenus','/admin/contenus'),('events','À la une','/admin/a-la-une'),('downloads','Téléchargements','/admin/telechargements'),('trailers','Bandes-annonces','/admin/bandes-annonces')] %}<a class="pill {{ 'on' if tab==k }}" href="{{ h }}">{{ l }}</a>{% endfor %}<a class="pill" href="/admin/export">⬇ Sauvegarde</a></div>{% block a %}{% endblock %}{% endblock %}"""

T["admin.html"] = r"""{% extends "adm.html" %}{% block a %}
<div class="card" style="border-color:{{ '#2e8b57' if meta.durable else '#e5233b' }}"><b>{{ '✅' if meta.durable else '⚠️' }} Stockage : {{ meta.storage }}{{ ' (permanent)' if meta.durable and meta.storage=='PostgreSQL' else '' }}</b>
<small class="mut" style="display:block;margin-top:4px">{% if meta.durable %}Les données sont conservées après chaque mise à jour du code.{% else %}Stockage TEMPORAIRE : tout sera effacé à chaque mise à jour ou mise en veille. Dans Render &gt; Environment, ajoute la variable DATABASE_URL (adresse Postgres de Neon), puis redéploie.{% endif %}<br>Premier membre inscrit le {{ meta.since or '—' }} · Dernière sauvegarde : {{ meta.backup[:10] if meta.backup else 'jamais' }}{% if meta.old %} — <a href="/admin/export" style="color:var(--or)">faire une sauvegarde</a>{% endif %}</small></div>
<div class="card"><b>🗂️ Médias : {{ meta.mmb }} Mo · {{ meta.mfiles }} fichier{{ 's' if meta.mfiles>1 }}</b>{% if meta.dbmb %}<small class="mut" style="display:block;margin-top:4px">Base de données entière : {{ meta.dbmb }} Mo</small>{% endif %}
<small class="mut" style="display:block;margin-top:4px">Les photos, vidéos et vocaux sont supprimés après {{ MEDIA_DAYS }} jours (le texte reste), ou plus tôt si le total dépasse {{ MEDIA_CAP_MB }} Mo. Nettoyage automatique toutes les heures.</small>
<form method="post" action="/admin/action" style="margin-top:8px">{{ csrf() }}<button class="btn s g" name="act" value="purge">Nettoyer maintenant</button></form></div>
<div class="kpi">{% for k,l in [('users','Membres'),('u1','Inscrits 24 h'),('u7','Inscrits 7 j'),('posts','Posts'),('comments','Commentaires'),('groups','Groupes'),('reports','Signalements'),('banned','Suspendus'),('msgs','Messages')] %}<div style="margin-bottom:8px"><b>{{ stats[k] }}</b><small>{{ l }}</small></div>{% endfor %}</div>
<h2>Signalements ouverts</h2>
{% for x in rows %}<div class="card"><small class="mut">{{ x.r.kind }} · {{ x.r.reason }} · par {{ x.r.rep }} · {{ ago(x.r.created) }}</small>
{% if x.t %}<p style="margin:8px 0"><a href="/admin/membre/{{ x.t.uid }}" style="color:var(--or2)"><b>{{ x.t.username }}</b></a> : {{ x.t.body[:300] }}</p>{% else %}<p class="mut">Contenu déjà supprimé.</p>{% endif %}
<form method="post" action="/admin/action" class="row" style="flex-wrap:wrap">{{ csrf() }}<input type="hidden" name="id" value="{{ x.r.id }}"><button class="btn s g" name="act" value="dismiss">Ignorer</button>{% if x.t and x.r.kind!='user' %}<button class="btn s" name="act" value="rdelete">Supprimer le contenu</button>{% endif %}{% if x.t %}<button class="btn s r" name="act" value="rban">Suspendre l'auteur</button>{% endif %}</form></div>
{% else %}<p class="mut" style="text-align:center">Aucun signalement ouvert. 🎉</p>{% endfor %}
<h2>Journal des actions</h2><div class="grp">{% for l in log %}<div class="it"><span class="grow">{{ l.adm }} · {{ l.action }} {{ l.target }}<small>{{ l.detail }} · {{ ago(l.created) }}</small></span></div>{% else %}<div class="it mut">Aucune action pour l'instant.</div>{% endfor %}</div>{% endblock %}"""

T["admin_members.html"] = r"""{% extends "adm.html" %}{% block a %}<form class="row" style="margin:6px 0"><input name="q" value="{{ q }}" placeholder="Pseudo ou e-mail…"><button class="btn">{{ ic('search',18) }}</button></form>
<div class="grp">{% for r in rows %}<a class="it" href="/admin/membre/{{ r.id }}">{{ ava(r.username,36) }}<span class="grow">{{ r.username }}{% if r.is_admin %} <b style="color:var(--or)">· admin</b>{% endif %}{% if r.banned %} <b style="color:#ff7b8d">· suspendu</b>{% endif %}<small>{{ r.email or 'pas d\'e-mail' }} · {{ r.np }} posts · {{ r.nr }} signalement(s) · inscrit le {{ r.created[:10] }}</small></span></a>{% else %}<div class="it mut">Aucun membre.</div>{% endfor %}</div>{% endblock %}"""

T["admin_user.html"] = r"""{% extends "adm.html" %}{% block a %}<div class="card row">{{ ava(u.username,56) }}<div class="grow"><b style="font-size:1.1rem">{{ u.username }}</b>{% if adm %} <b style="color:var(--or)">· admin</b>{% endif %}<small class="mut" style="display:block">{{ u.email or 'pas d\'e-mail' }} · inscrit le {{ u.created[:10] }} · {{ reps }} signalement(s)</small></div></div>
{% if u.bio %}<p class="mut">{{ u.bio }}</p>{% endif %}
{% if u.av or u.bv or u.p_status %}<form method="post" action="/admin/action">{{ csrf() }}<input type="hidden" name="id" value="{{ u.id }}"><button class="btn s g" name="act" value="rm_images">Retirer photo / bannière / statut</button></form>{% endif %}
{% if not adm and u.id!=me.id %}<div class="card">{% if u.banned %}<p style="margin-top:0">🚫 Suspendu{% if u.ban_reason %} — {{ u.ban_reason }}{% endif %}</p><form method="post" action="/admin/action">{{ csrf() }}<input type="hidden" name="id" value="{{ u.id }}"><button class="btn s" name="act" value="unban">Rétablir le compte</button></form>
{% else %}<form method="post" action="/admin/action">{{ csrf() }}<input type="hidden" name="id" value="{{ u.id }}"><label style="margin-top:0">Motif de suspension (affiché à la personne)</label><input name="reason" maxlength="120" placeholder="Ex : harcèlement"><p><button class="btn s r" name="act" value="ban">Suspendre ce compte</button></p></form>{% endif %}
<form method="post" action="/admin/action" onsubmit="return confirm('Supprimer définitivement ce compte et tout son contenu ?')">{{ csrf() }}<input type="hidden" name="id" value="{{ u.id }}"><label>Supprimer le compte : retape le pseudo pour confirmer</label><input name="confirm" placeholder="{{ u.username }}" autocomplete="off"><p><button class="btn s g" name="act" value="delete_user">Supprimer définitivement</button></p></form></div>{% endif %}
{% for title,items,kind in [('Posts',posts,'post'),('Commentaires',comments,'comment'),('Messages de groupe',cposts,'cpost')] %}<h2>{{ title }}</h2>
{% for p in items %}<div class="card row"><span class="grow">{% if p.mk %}<a href="/api/groupe/media/{{ p.id }}" target="_blank" style="color:var(--or2)">📎 {{ p.mk }}</a> {% elif p.has_img==1 %}<a href="/api/post/media/{{ p.id }}" target="_blank" style="color:var(--or2)">📎 photo</a> {% endif %}{{ p.body[:200] }}<small class="mut" style="display:block">{{ ago(p.created) }}</small></span><form method="post" action="/admin/action">{{ csrf() }}<input type="hidden" name="id" value="{{ p.id }}"><input type="hidden" name="kind" value="{{ kind }}"><button class="btn s g" name="act" value="delete_content">{{ ic('trash',14) }}</button></form></div>{% else %}<p class="mut">Rien.</p>{% endfor %}{% endfor %}{% endblock %}"""

T["admin_content.html"] = r"""{% extends "adm.html" %}{% block a %}{% for title,items,kind in [('Derniers posts',posts,'post'),('Derniers commentaires',comments,'comment'),('Derniers messages de groupe',cposts,'cpost')] %}<h2>{{ title }}</h2>
{% for p in items %}<div class="card"><small class="mut"><a href="/admin/membre/{{ p.uid }}" style="color:var(--or2)">{{ p.username }}</a> · {{ ago(p.created) }}</small><p style="margin:6px 0">{% if p.mk %}<a href="/api/groupe/media/{{ p.id }}" target="_blank" style="color:var(--or2)">📎 {{ p.mk }}</a> {% elif p.has_img==1 %}<a href="/api/post/media/{{ p.id }}" target="_blank" style="color:var(--or2)">📎 photo</a> {% endif %}{{ p.body[:250] }}</p>
<form method="post" action="/admin/action">{{ csrf() }}<input type="hidden" name="id" value="{{ p.id }}"><input type="hidden" name="kind" value="{{ kind }}"><button class="btn s g" name="act" value="delete_content">Supprimer</button></form></div>{% else %}<p class="mut">Rien.</p>{% endfor %}{% endfor %}{% endblock %}"""

T["customize.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Personnaliser mon profil</h1>
<div class="pcard" id="pcard" style="--c:{{ me.p_color }}"><div class="pbanner bn-sunset" id="pb"><div class="fxbox" id="fxb"></div></div>
<div class="pmeta"><div id="pav">{{ ava(me.username,92) }}</div><h1 class="nm" id="pnm">{{ me.username }}</h1><br><span class="pstatus" id="pst" hidden></span></div></div>
<form method="post" id="cf" style="margin-top:14px">{{ csrf() }}
<div class="sec">Photo et bannière</div><div class="grp" style="padding:14px 16px"><div class="row" style="flex-wrap:wrap"><button type="button" class="btn s" onclick="$('fa').click()">📷 Changer la photo</button><button type="button" class="btn s g" onclick="$('fb').click()">🖼 Changer la bannière</button>
{% if me.av %}<button class="btn s g" formaction="/profil/photo/supprimer" name="kind" value="avatar" onclick="return confirm('Supprimer la photo ?')">Retirer la photo</button>{% endif %}{% if me.bv %}<button class="btn s g" formaction="/profil/photo/supprimer" name="kind" value="banner" onclick="return confirm('Supprimer la bannière ?')">Retirer la bannière</button>{% endif %}</div>
<input type="file" id="fa" accept="image/*" hidden><input type="file" id="fb" accept="image/*" hidden><small id="um" class="hint" style="margin-top:8px">Les images sont recadrées et réduites automatiquement (les GIF animés deviennent fixes).</small></div>
<div class="sec">Cadre animé de la photo</div><div class="chips">{% for k,l in RINGS.items() %}<label class="chp"><input type="radio" name="ring" value="{{ k }}" {{ 'checked' if me.p_ring==k }}><span>{{ l }}</span></label>{% endfor %}</div>
<div class="sec">Effet animé sur la bannière</div><div class="chips">{% for k,l in FXS.items() %}<label class="chp"><input type="radio" name="fx" value="{{ k }}" {{ 'checked' if me.p_fx==k }}><span>{{ l }}</span></label>{% endfor %}</div>
<div class="sec">Style du pseudo</div><div class="chips">{% for k,l in NAMES.items() %}<label class="chp"><input type="radio" name="nm" value="{{ k }}" {{ 'checked' if me.p_name==k }}><span>{{ l }}</span></label>{% endfor %}</div>
<div class="sec">Bannière</div><div class="chips">{% for k,l in BANNERS.items() %}<label class="chp"><input type="radio" name="banner" value="{{ k }}" {{ 'checked' if me.p_banner==k }}><span>{{ l }}</span></label>{% endfor %}<label class="chp" id="bimg" {{ '' if me.bv else 'hidden' }}><input type="radio" name="banner" value="img" {{ 'checked' if me.p_banner=='img' }}><span>Mon image</span></label></div>
<div class="sec">Couleur d'accent</div><div class="grp" style="padding:12px 16px"><input type="color" name="color" value="{{ me.p_color }}" style="height:46px;padding:4px"></div>
<div class="sec">Type d'otaku</div><div class="chips"><label class="chp"><input type="radio" name="otype" value="" {{ 'checked' if not me.otype }}><span>Aucun</span></label>{% for o in OTYPES %}<label class="chp"><input type="radio" name="otype" value="{{ o }}" {{ 'checked' if me.otype==o }}><span>{{ o }}</span></label>{% endfor %}</div>
<div class="sec">Statut</div><div class="grp" style="padding:12px 16px"><input name="pstatus" maxlength="60" value="{{ me.p_status }}" placeholder="Ex : en plein marathon One Piece 🏴‍☠️"></div>
<p><button class="btn w">Enregistrer mon profil</button></p></form>
<script>var F=$('cf'),U={{ me.username|tojson }},IMG={{ ('/u/'+me.username+'/banner?v='+me.bv|string)|tojson if me.bv else 'null' }},csrf="{{ session.get('csrf','') }}";
function val(n){var e=F.querySelector('input[name='+n+']:checked');return e?e.value:''}
function prev(){var c=F.color.value,a=$('pav').firstElementChild;$('pcard').style.setProperty('--c',c);a.className='avw ring-'+val('ring');a.style.setProperty('--c',c);$('pnm').className='nm nm-'+val('nm');
 var b=$('pb'),k=val('banner');b.className='pbanner bn-'+k;b.style.backgroundImage=(k=='img'&&IMG)?'url('+IMG+')':'';var s=F.pstatus.value.trim();$('pst').textContent=s;$('pst').hidden=!s}
var lastfx='';function pfx(){var f=val('fx');if(f!=lastfx){lastfx=f;fx($('fxb'),f)}}
F.addEventListener('change',function(){prev();pfx()});F.addEventListener('input',prev);prev();pfx();
function shrink(file,w,h,max){return new Promise(function(res,rej){var fr=new FileReader();fr.onerror=function(){rej(new Error('Lecture impossible'))};fr.onload=function(){var im=new Image();im.onerror=function(){rej(new Error('Image illisible'))};
 im.onload=function(){var c=document.createElement('canvas');c.width=w;c.height=h;var x=c.getContext('2d'),r=Math.max(w/im.width,h/im.height),sw=w/r,sh=h/r;x.fillStyle='#000';x.fillRect(0,0,w,h);x.drawImage(im,(im.width-sw)/2,(im.height-sh)/2,sw,sh,0,0,w,h);
  (function t(q){c.toBlob(function(b){if(!b)return rej(new Error('Conversion impossible'));if(b.size>max&&q>.35)return t(q-.1);b.size>max?rej(new Error('Image trop lourde')):res(b)},'image/jpeg',q)})(.85)};im.src=fr.result};fr.readAsDataURL(file)})}
function up(kind,file,w,h,max){if(!file)return;$('um').textContent='Envoi en cours…';
 shrink(file,w,h,max).then(function(b){return fetch('/api/img/'+kind,{method:'POST',headers:{'Content-Type':'image/jpeg','X-CSRF':csrf},body:b})}).then(function(r){return r.json()}).then(function(d){if(d.error)throw new Error(d.error);
  if(kind=='avatar'){var o=$('pav').querySelector('.ava'),im=new Image();im.className='ava';im.style.cssText='width:92px;height:92px';im.src='/u/'+U+'/avatar?v='+d.v;o.replaceWith(im)}
  else{IMG='/u/'+U+'/banner?v='+d.v;$('bimg').hidden=false;$('bimg').querySelector('input').checked=true;prev()}
  $('um').textContent='✅ Image enregistrée'}).catch(function(e){$('um').textContent='❌ '+e.message})}
$('fa').onchange=function(){up('avatar',this.files[0],256,256,65000)};$('fb').onchange=function(){up('banner',this.files[0],900,300,160000)};</script>{% endblock %}"""

T["setup.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><div class="logo" style="font-size:2.4rem">Senpai<b>X</b></div><h1 style="margin:8px 0">Installation</h1>
<p class="mut">Aucun administrateur n'existe encore. Entre un pseudo et un mot de passe (10 caractères minimum). Si ce pseudo existe déjà, il devient administrateur après vérification de son mot de passe.</p>
<form method="post" class="card">{{ csrf() }}<label>Pseudo</label><input name="username" required maxlength="20" autocapitalize="none" autocomplete="username" value="{{ v.get('username','') }}">
<label>Mot de passe</label><input type="password" name="password" required autocomplete="new-password"><label>Confirmer <span style="font-weight:400">(nouveau compte seulement)</span></label><input type="password" name="password2" autocomplete="new-password">
<p><button class="btn w">Devenir administrateur</button></p></form><p class="mut" style="font-size:.78rem">Cette page disparaît dès qu'un administrateur existe. Fais-le tout de suite après le déploiement.</p></div>{% endblock %}"""

T["err.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><h1 style="font-size:4rem;margin:0;color:var(--or)">{{ code }}</h1><p class="mut">{{ msg or "Oups, cette page n'est pas disponible." }}</p><a class="btn" href="/">Retour à l'accueil</a></div>{% endblock %}"""

app.jinja_loader = DictLoader(T)
app.jinja_env.globals["css"] = CSS
app.jinja_env.globals.update(PLATS=PLATS, host_of=host_of, REACTS=REACTS, MEDIA_MAX=MEDIA_MAX, snip=snip, MEDIA_DAYS=MEDIA_DAYS, MEDIA_CAP_MB=MEDIA_CAP_MB, EVT_KINDS=EVT_KINDS, fdate=fdate, poll_of=poll_of, gav=gav)
app.jinja_env.globals["js"] = '\nfunction $(x){return document.getElementById(x)}\nfunction esc(s){return String(s==null?\'\':s).replace(/[&<>"\']/g,function(c){return{\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]})}\nvar JK=\'https://api.jikan.moe/v4\',DAYS=[\'monday\',\'tuesday\',\'wednesday\',\'thursday\',\'friday\',\'saturday\',\'sunday\'];\nfunction nerr(e){var m=e&&e.message||String(e);return e&&e.name==\'AbortError\'?\'délai dépassé\':(/Failed to fetch|Load failed|NetworkError/i.test(m)?\'connexion impossible\':m)}\nfunction req(u,o){var ac=new AbortController(),t=setTimeout(function(){ac.abort()},9000);o=o||{};o.signal=ac.signal;\n return fetch(u,o).then(function(r){clearTimeout(t);if(!r.ok)throw new Error(\'HTTP \'+r.status);return r.json()}).catch(function(e){clearTimeout(t);throw new Error(nerr(e))})}\nfunction get(u){return req(u)}\nfunction AL(q,v){return req(\'https://graphql.anilist.co\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({query:q,variables:v||{}})}).then(function(d){if(d.errors)throw new Error(d.errors[0].message);return d.data})}\nfunction purge(){Object.keys(localStorage).forEach(function(x){if(x.indexOf(\'c:\')==0)localStorage.removeItem(x)})}\nfunction cache(k,ttl,fn){k=\'c:\'+k;var c=null;try{c=JSON.parse(localStorage.getItem(k))}catch(e){}\n if(c&&Date.now()-c.t<ttl*6e4)return Promise.resolve(c.d);\n return fn().then(function(d){try{localStorage.setItem(k,JSON.stringify({t:Date.now(),d:d}))}catch(e){purge()}return d},function(e){if(c)return c.d;throw e})}\nfunction strip(s){return String(s||\'\').replace(/<[^>]*>/g,\' \').replace(/\\s+/g,\' \').trim()}\nfunction uniq(l){var s={};return l.filter(function(x){return x.id&&!s[x.id]&&(s[x.id]=1)})}\nfunction shuf(a){for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1)),t=a[i];a[i]=a[j];a[j]=t}return a}\nvar MF=\'idMal isAdult title{romaji english} coverImage{large} averageScore seasonYear startDate{year} episodes chapters format genres studios(isMain:true){nodes{name}} description(asHtml:false)\';\nfunction alm(m){return{id:m.idMal,title:(m.title&&(m.title.english||m.title.romaji))||\'?\',img:(m.coverImage&&m.coverImage.large)||\'\',score:m.averageScore?Math.round(m.averageScore)/10:null,\n year:m.seasonYear||(m.startDate&&m.startDate.year)||null,eps:m.episodes||m.chapters||null,type:m.format||\'\',genres:(m.genres||[]).join(\', \'),\n studio:((m.studios&&m.studios.nodes)||[]).map(function(n){return n.name}).join(\', \'),syn:strip(m.description)}}\nfunction nm(it){var im=(it.images&&it.images.jpg)||{};return{id:it.mal_id,title:it.title_english||it.title||\'?\',img:im.large_image_url||im.image_url||\'\',score:it.score,\n year:it.year||(it.aired&&it.aired.prop&&it.aired.prop.from&&it.aired.prop.from.year),syn:(it.synopsis||\'\').trim(),type:it.type,eps:it.episodes||it.chapters,\n genres:(it.genres||[]).map(function(g){return g.name}).join(\', \'),studio:(it.studios||[]).map(function(g){return g.name}).join(\', \')}}\nfunction fin(a){return uniq(a.filter(function(m){return m&&m.idMal&&!m.isAdult}).map(alm))}\nfunction season(){var d=new Date();return{s:[\'WINTER\',\'SPRING\',\'SUMMER\',\'FALL\'][Math.floor(d.getMonth()/3)],y:d.getFullYear()}}\nfunction dayRange(day){var n=new Date(),tm=(n.getDay()+6)%7,d=new Date(n.getFullYear(),n.getMonth(),n.getDate()+DAYS.indexOf(day)-tm),a=Math.floor(d.getTime()/1000);return[a-1,a+86400]}\nvar STOP={moi:1,quand:1,que:1,qui:1,les:1,des:1,une:1,dans:1,sur:1,pour:1,par:1,avec:1,mon:1,mes:1,son:1,ses:1,est:1,sont:1,the:1,and:1,anime:1,manga:1,saison:1};\nfunction kws(q){return q.toLowerCase().normalize(\'NFD\').replace(/[\\u0300-\\u036f]/g,\'\').replace(/[^a-z0-9\\s]/g,\' \').split(/\\s+/).filter(function(w){return w.length>2&&!STOP[w]}).map(function(w){return/^reincarn/.test(w)?\'reincarnated\':w})}\nfunction alQ(q,sort){return AL(\'query($q:String){Page(page:1,perPage:30){media(type:ANIME,isAdult:false,search:$q,sort:\'+sort+\'){\'+MF+\'}}}\',{q:q}).then(function(d){return fin(d.Page.media)})}\nfunction srch(q){return alQ(q,\'SEARCH_MATCH\').then(function(l){if(l.length)return l;var k=kws(q);if(!k.length)return[];\n return alQ(k.join(\' \'),\'SEARCH_MATCH\').then(function(l2){if(l2.length)return l2;\n  return Promise.all(k.map(function(w){return alQ(w,\'POPULARITY_DESC\').catch(function(){return[]})})).then(function(arr){var sc={},by={};arr.forEach(function(a){a.forEach(function(m){by[m.id]=m;sc[m.id]=(sc[m.id]||0)+1})});\n   return Object.keys(by).map(function(i){return by[i]}).sort(function(a,b){return sc[b.id]-sc[a.id]})})})})}\nfunction alList(kind,o,pg){\n if(kind==\'search\')return srch(o.q||\'\');\n if(kind==\'planning\'){var r=dayRange(o.day||\'monday\');return AL(\'query($a:Int,$b:Int){Page(perPage:50){airingSchedules(airingAt_greater:$a,airingAt_lesser:$b,sort:TIME){media{\'+MF+\'}}}}\',{a:r[0],b:r[1]}).then(function(d){return fin(d.Page.airingSchedules.map(function(x){return x.media}))})}\n var v={p:pg,t:kind==\'manga\'?\'MANGA\':\'ANIME\'},decl=\'$p:Int,$t:MediaType\',args=\'type:$t,isAdult:false\',sort=\'POPULARITY_DESC\';\n if(kind==\'saison\'){var z=season();v.s=z.s;v.y=z.y;decl+=\',$s:MediaSeason,$y:Int\';args+=\',season:$s,seasonYear:$y\'}\n else if(kind==\'top\'){sort=\'SCORE_DESC\';args+=\',popularity_greater:30000\'}\n else if(kind==\'manga\'){sort=\'SCORE_DESC\';args+=\',popularity_greater:10000\'}\n else if(kind==\'trend\')sort=\'TRENDING_DESC\';\n else if(kind==\'explore\'){sort=o.sort||\'POPULARITY_DESC\';args+=\',popularity_greater:3000\';if(o.genre){v.g=o.genre;decl+=\',$g:String\';args+=\',genre:$g\'}if(o.year){v.a=o.year*10000;v.b=o.year*10000+10000;decl+=\',$a:FuzzyDateInt,$b:FuzzyDateInt\';args+=\',startDate_greater:$a,startDate_lesser:$b\'}if(o.min){v.m=+o.min*10;decl+=\',$m:Int\';args+=\',averageScore_greater:$m\'}}\n  return AL(\'query(\'+decl+\'){Page(page:$p,perPage:30){media(\'+args+\',sort:\'+sort+\'){\'+MF+\'}}}\',v).then(function(d){return fin(d.Page.media)})}\nfunction jkList(kind,o,pg){\n var P={saison:\'/seasons/now?limit=24&sfw=true&page=\'+pg,trend:\'/seasons/now?limit=14&sfw=true\',explore:\'/top/anime?limit=25&sfw=true&page=\'+pg,top:\'/top/anime?limit=25&sfw=true&page=\'+pg,manga:\'/top/manga?limit=24&sfw=true&page=\'+pg,\n  planning:\'/schedules?filter=\'+(o.day||\'monday\')+\'&limit=24&sfw=true\',search:\'/anime?q=\'+encodeURIComponent(o.q||\'\')+\'&limit=12&sfw=true&order_by=popularity\'};\n return get(JK+P[kind]).then(function(d){return uniq((d.data||[]).map(nm))})}\nfunction L(kind,o){o=o||{};var pg=o.page||1;\n return cache([kind,o.day||\'\',pg,o.q||\'\',o.genre||\'\',o.year||\'\',o.sort||\'\',o.min||\'\'].join(\'|\'),30,function(){\n  return alList(kind,o,pg).then(function(l){if(!l.length&&kind!=\'search\')throw new Error(\'liste vide\');return l}).catch(function(e1){return jkList(kind,o,pg).catch(function(e2){throw new Error(\'AniList : \'+e1.message+\' · Jikan : \'+e2.message)})})})}\nfunction M(id,kind){return cache(\'m\'+kind+id,60,function(){\n return AL(\'query($i:Int,$t:MediaType){Media(idMal:$i,type:$t){\'+MF+\' nextAiringEpisode{airingAt episode} trailer{id site} characters(perPage:10,sort:ROLE){nodes{name{full} image{medium}}}}}\',{i:id,t:kind==\'manga\'?\'MANGA\':\'ANIME\'}).then(function(d){var x=d.Media,m=alm(x);\n  m.next=x.nextAiringEpisode||null;m.trailer=x.trailer&&x.trailer.site==\'youtube\'?\'https://youtu.be/\'+x.trailer.id:\'\';m.chars=x.characters.nodes.map(function(c){return{title:c.name.full,img:c.image.medium}});return m})\n .catch(function(e1){return get(JK+\'/\'+(kind==\'manga\'?\'manga\':\'anime\')+\'/\'+id).then(function(d){var m=nm(d.data);m.trailer=(d.data.trailer||{}).url||\'\';m.chars=[];\n  if(kind==\'manga\')return m;return get(JK+\'/anime/\'+id+\'/characters\').then(function(c){m.chars=(c.data||[]).slice(0,10).map(function(x){return{title:x.character.name,img:x.character.images.jpg.image_url}});return m}).catch(function(){return m})})})})}\nfunction skI(n){var h=\'\';for(var i=0;i<n;i++)h+=\'<div class="tile"><div class="sk" style="aspect-ratio:2/3;border-radius:14px"></div><div class="sk" style="height:10px;margin-top:6px;width:70%"></div></div>\';return h}\nfunction skT(n){return\'<div class="grid">\'+skI(n)+\'</div>\'}\nfunction skC(n){var h=\'\';for(var i=0;i<n;i++)h+=\'<div class="st"><div class="sk" style="width:72px;height:72px;border-radius:50%"></div></div>\';return h}\nfunction tileItems(l,k){return l.map(function(m){return\'<a class="tile" href="/serie/\'+m.id+(k==\'manga\'?\'?k=manga\':\'\')+\'"><img src="\'+esc(m.img)+\'" loading="lazy" alt="">\'+(m.score?\'<small>★ \'+m.score+\'</small>\':\'\')+\'<b>\'+esc(m.title)+\'</b></a>\'}).join(\'\')}\nfunction tiles(l,k){return l.length?\'<div class="grid">\'+tileItems(l,k)+\'</div>\':\'<p class="mut" style="text-align:center">Aucun résultat.</p>\'}\nfunction rings(l){return l.map(function(m){return\'<a class="st" href="/serie/\'+m.id+\'"><div class="ring"><img src="\'+esc(m.img)+\'" alt="" loading="lazy"></div><span>\'+esc(m.title)+\'</span></a>\'}).join(\'\')}\nfunction posters(l){return l.map(function(m){return\'<a class="st" style="width:96px" href="/serie/\'+m.id+\'"><img src="\'+esc(m.img)+\'" alt="" loading="lazy" style="border-radius:14px;aspect-ratio:2/3;border:0;object-fit:cover;width:100%"><span>\'+esc(m.title)+\'</span></a>\'}).join(\'\')}\nfunction fail(el,e){el.innerHTML=\'<p class="mut" style="grid-column:1/-1;text-align:center;padding:14px">Sources injoignables.<br><small>\'+esc(e&&e.message||e)+\'</small><br><a href="" style="color:var(--or)">Réessayer</a> · <a href="/diagnostic" style="color:var(--or)">Diagnostic</a></p>\'}\n\nfunction dur(s){s=Math.max(0,s);var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?d+\' j \'+h+\' h\':h?h+\' h \'+m+\' min\':m+\' min\'}\nfunction NX(ids){if(!ids.length)return Promise.resolve({});return cache(\'nx|\'+ids.join(\',\'),30,function(){\n return AL(\'query($i:[Int]){Page(perPage:50){media(idMal_in:$i,type:ANIME){idMal title{romaji english} coverImage{large} nextAiringEpisode{airingAt episode}}}}\',{i:ids.slice(0,50)}).then(function(d){var o={};\n  d.Page.media.forEach(function(m){if(m.nextAiringEpisode)o[m.idMal]={at:m.nextAiringEpisode.airingAt,ep:m.nextAiringEpisode.episode,title:m.title.english||m.title.romaji,img:m.coverImage.large}});return o})})}\n\nfunction fx(b,t){b.innerHTML=\'\';var m={sakura:[\'🌸\',\'fall\'],stars:[\'✨\',\'twk\'],snow:[\'❄️\',\'fall\'],flames:[\'🔥\',\'rise\']}[t];if(!m||(window.matchMedia&&matchMedia(\'(prefers-reduced-motion: reduce)\').matches))return;\n for(var i=0;i<16;i++){var e=document.createElement(\'span\');e.className=\'fxp \'+m[1];e.textContent=m[0];e.style.left=(Math.random()*94)+\'%\';e.style.fontSize=(10+Math.random()*14)+\'px\';\n  e.style.animationDuration=(4+Math.random()*6)+\'s\';e.style.animationDelay=(-Math.random()*8)+\'s\';if(m[1]==\'twk\')e.style.top=(Math.random()*85)+\'%\';b.appendChild(e)}}\n'

def ensure_admin():
    n, p = os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
    if not n:
        return
    if not re.fullmatch(r"[\w.\-]{3,20}", n) or len(p) < 10 or pw_problem(p, n):
        log.error("ADMIN_USERNAME/ADMIN_PASSWORD invalides (pseudo 3-20 caractères, mot de passe 10+ caractères non courant) : compte admin non créé.")
        return
    con = Conn()
    try:
        u = con.execute("SELECT * FROM users WHERE username=?", (n,)).fetchone()
        if not u:
            con.execute("INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,1,?)", (n, generate_password_hash(p), now()))
            log.info("Compte administrateur créé : %s", n)
        else:
            if not u["is_admin"]:
                con.execute("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
            if not check_password_hash(u["pw"], p):
                con.execute("UPDATE users SET pw=?,sv=sv+1 WHERE id=?", (generate_password_hash(p), u["id"]))
                log.info("Mot de passe administrateur mis à jour.")
        con.commit()
    finally:
        con.close()


READY = {"ok": False, "t": 0.0, "err": ""}
BOOT_LOCK = threading.Lock()


def mask(msg):
    pw = urllib.parse.urlsplit(DATABASE_URL).password if PG else None
    return msg.replace(pw, "***") if pw else msg


def boot():
    with BOOT_LOCK:
        if READY["ok"]:
            return
        READY["t"] = time.time()
        try:
            init_db()
            if not DURABLE:
                log.warning("⚠️ STOCKAGE TEMPORAIRE : " + ("le pilote Postgres n'a pas pu être installé" if DATABASE_URL else "DATABASE_URL absente") + ", les données seront effacées à chaque mise à jour.")
            ensure_admin()
            READY.update(ok=True, err="")
            log.info("Base prête (%s)", "PostgreSQL" if PG else "SQLite")
        except Exception as e:
            READY["err"] = type(e).__name__ + (f" [{e.pgcode}]" if getattr(e, "pgcode", None) else "")
            log.error("Initialisation de la base impossible (%s). Vérifie DATABASE_URL. Détail : %s", READY["err"], mask(str(e))[:300])


boot()


@app.before_request
def ensure_ready():
    if not READY["ok"] and request.path != "/healthz" and time.time() - READY["t"] > 15:
        boot()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

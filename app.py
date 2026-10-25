"""
SenpaiX v21 — réseau social otaku, look "Otakun Mobile" avec Avatars Personnalisés.
Flask + Postgres (Render/Neon) ou SQLite (local).
"""
import os, re, json, time, random, sqlite3, secrets, logging, threading
import urllib.request, urllib.parse, urllib.error, ssl
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import (Flask, request, redirect, session, render_template, flash, abort, g,
                   jsonify, url_for)
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
TOURNAMENT_HOUR_UTC = 18
Q_COUNT, Q_TIME = 10, 15
STATUSES = {"cours": "En cours", "voir": "À voir", "fini": "Terminé"}
REASONS = ["Spam ou arnaque", "Harcèlement ou insultes", "Spoiler non signalé", "Contenu inapproprié", "Autre"]
GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Horror", "Mahou Shoujo", "Mecha", "Music", "Mystery", "Psychological", "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller"]
SORTS = {"POPULARITY_DESC": "Popularité", "SCORE_DESC": "Meilleure note", "TRENDING_DESC": "Tendance", "START_DATE_DESC": "Plus récents"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("senpaix")


def secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f:
            f.write(secrets.token_hex(32))
    return open(KEY_FILE).read().strip()


PROD = bool(os.environ.get("RENDER")) or os.environ.get("SENPAIX_PROD") == "1"
app = Flask(__name__)
app.secret_key = secret()
if PROD:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PROD,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30, MAX_CONTENT_LENGTH=64 * 1024)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,pw TEXT NOT NULL,
 bio TEXT DEFAULT '',avatar TEXT DEFAULT '',theme TEXT DEFAULT 'dark',notify INTEGER DEFAULT 1,privacy TEXT DEFAULT 'public',
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
CREATE TABLE IF NOT EXISTS favs(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,mal_id INTEGER,title TEXT,img TEXT,
 PRIMARY KEY(user_id,mal_id));
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
 mode TEXT,good INTEGER,total INTEGER,ms INTEGER,day TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocks(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,blocked_id INTEGER REFERENCES users(id) ON DELETE CASCADE,PRIMARY KEY(user_id,blocked_id));
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,reporter_id INTEGER REFERENCES users(id) ON DELETE CASCADE,kind TEXT,target_id INTEGER,reason TEXT,status TEXT DEFAULT 'ouvert',created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY,admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE,action TEXT,target TEXT,detail TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifs(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
 text TEXT,link TEXT DEFAULT '/',read INTEGER DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY,s INTEGER REFERENCES users(id) ON DELETE CASCADE,
 r INTEGER REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,read INTEGER DEFAULT 0,created TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip().replace("postgres://", "postgresql://", 1)
PG = DATABASE_URL.startswith("postgresql")
if PG:
    import psycopg2, psycopg2.extras
    DBError = psycopg2.Error
else:
    DBError = sqlite3.Error
ID_TABLES = {"users", "posts", "comments", "communities", "cposts", "scores", "notifs", "msgs", "reports", "admin_log"}
FAV_UPSERT = (" ON CONFLICT (user_id,mal_id) DO UPDATE SET title=EXCLUDED.title,img=EXCLUDED.img,status=EXCLUDED.status,ep=EXCLUDED.ep,"
              "total=EXCLUDED.total,score=EXCLUDED.score,kind=EXCLUDED.kind,updated=EXCLUDED.updated")
MIGRATIONS = (("users", "email TEXT DEFAULT ''"), ("users", "avatar TEXT DEFAULT ''"), ("users", "recovery TEXT DEFAULT ''"), 
              ("users", "banned INTEGER DEFAULT 0"), ("users", "is_admin INTEGER DEFAULT 0"), ("users", "ban_reason TEXT DEFAULT ''"),
              ("favs", "status TEXT DEFAULT 'voir'"), ("favs", "ep INTEGER DEFAULT 0"), ("favs", "total INTEGER DEFAULT 0"),
              ("favs", "score INTEGER DEFAULT 0"), ("favs", "kind TEXT DEFAULT 'anime'"), ("favs", "updated TEXT DEFAULT ''"))


def pg_sql(sql):
    sql = sql.replace("%", "%%").replace("?", "%s")
    if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO FAVS"):
        sql = re.sub(r"INSERT OR REPLACE INTO", "INSERT INTO", sql, 1, flags=re.I) + FAV_UPSERT
    return re.sub(r"\bLIKE\b", "ILIKE", sql)


class Res:
    def __init__(self, c, lid=None):
        self.c, self.lastrowid, self.rowcount = c, lid, c.rowcount
    def fetchone(self): return self.c.fetchone()
    def fetchall(self): return self.c.fetchall()
    def __iter__(self): return iter(self.c.fetchall())


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
        c.execute(sql + (" RETURNING id" if ret else ""), args)
        return Res(c, c.fetchone()[0] if ret else None)

    def commit(self): self.con.commit()
    def rollback(self): self.con.rollback()
    def close(self): self.con.close()


def db():
    if "db" not in g:
        try:
            g.db = Conn()
        except DBError:
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


def rowd(r): return {k: r[k] for k in r.keys()}


def init_db():
    con = Conn()
    try:
        if PG:
            con.con.autocommit = True
            c = con.con.cursor()
            c.execute("SELECT pg_advisory_lock(727001)")
            try:
                for st in [x.strip() for x in SCHEMA.replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY").split(";") if x.strip()]:
                    c.execute(st)
                for t, col in MIGRATIONS:
                    c.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s" % (t, col))
            finally:
                c.execute("SELECT pg_advisory_unlock(727001)")
        else:
            con.con.executescript(SCHEMA)
            for t, col in MIGRATIONS:
                try:
                    con.con.execute("ALTER TABLE %s ADD COLUMN %s" % (t, col))
                except sqlite3.OperationalError:
                    pass
            con.commit()
    finally:
        con.close()


def next_tournament():
    n = datetime.now(timezone.utc)
    t = n.replace(hour=TOURNAMENT_HOUR_UTC, minute=0, second=0, microsecond=0)
    if t <= n: t += timedelta(days=1)
    return int((t - n).total_seconds())


def me():
    if "me" not in g:
        g.me = None
        uid = session.get("uid")
        if uid:
            u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if u and u["sv"] == session.get("sv") and not u["banned"]:
                g.me = u
            else:
                session.clear()
    return g.me


COMMON_PW = {"password", "motdepasse", "12345678", "123456789", "1234567890", "azerty123", "azertyuiop", "qwerty123", "qwertyuiop"}


def pw_problem(p, username=""):
    if not 8 <= len(p) <= 128: return "Mot de passe : 8 caractères minimum."
    if p.lower() in COMMON_PW or len(set(p)) < 4: return "Ce mot de passe est trop courant."
    return ""


def user_is_admin(u):
    return bool(u) and (bool(u["is_admin"]) or u["username"] in os.environ.get("SENPAIX_ADMINS", "").split(","))


def is_admin(): return user_is_admin(me())


def admin_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if not me(): return redirect("/connexion?next=" + urllib.parse.quote(request.path))
        if not is_admin(): abort(403)
        return fn(*a, **k)
    return w


def alog(action, target="", detail=""):
    db().execute("INSERT INTO admin_log(admin_id,action,target,detail,created) VALUES(?,?,?,?,?)", (uid(), action, str(target)[:80], str(detail)[:200], now()))


def toint(v, d=0, lo=0, hi=100000):
    try: return max(lo, min(hi, int(v)))
    except (TypeError, ValueError): return d


def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not me():
            flash("Connecte-toi d'abord.", "err")
            return redirect("/connexion?next=" + urllib.parse.quote(request.path))
        return fn(*a, **k)
    return w


def txt(v, n=2000): return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (v or "").strip())[:n]


def notify(uid, text, link="/"):
    u = db().execute("SELECT notify FROM users WHERE id=?", (uid,)).fetchone()
    if u and u["notify"] and (not me() or uid != me()["id"]):
        db().execute("INSERT INTO notifs(user_id,text,link,created) VALUES(?,?,?,?)", (uid, text, link, now()))


def ago(v):
    try: s = (datetime.now(timezone.utc) - datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds()
    except Exception: return ""
    for lim, div, u in ((60, 1, "à l'instant"), (3600, 60, "min"), (86400, 3600, "h"), (2592000, 86400, "j")):
        if s < lim: return u if div == 1 else f"{int(s // div)} {u}"
    return v[:10]


# Gestion des avatars (personnalisés ou initiales)
def ava(name, size=40, img_url=""):
    if img_url and img_url.startswith("https://"):
        return Markup(f'<img src="{img_url}" class="ava-img" style="width:{size}px;height:{size}px;" alt="{name}">')
    h = sum(map(ord, name)) % 360
    return Markup(f'<span class="ava" style="width:{size}px;height:{size}px;font-size:{size // 2.3}px;'
                  f'background:linear-gradient(135deg,hsl({h} 80% 55%),hsl({(h + 50) % 360} 85% 45%))">{name[:1].upper()}</span>')


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
    "dots": "M12 5h.01M12 12h.01M12 19h.01", "send": "M22 2L11 13M22 2l-7 20-4-9-9-4z", "star": "M12 2l3 7 7 .6-5.3 4.8 1.6 7.4L12 18l-6.3 3.8 1.6-7.4L2 9.6 9 9z",
}


def ic(name, size=22):
    return Markup(f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="{ICONS[name]}"/></svg>')


@app.context_processor
def inject():
    u = me()
    unread = 0
    if u:
        unread = db().execute("SELECT (SELECT COUNT(*) FROM notifs WHERE user_id=? AND read=0)+"
                              "(SELECT COUNT(*) FROM msgs WHERE r=? AND read=0)", (u["id"], u["id"])).fetchone()[0]
    # Fonction helper pour injecter l'avatar avec l'image perso si elle existe
    def safe_ava(name, size=40):
        avatar_url = u.get("avatar") if (u and u.get("username") == name) else ""
        return ava(name, size, avatar_url)
    return dict(STATUSES=STATUSES, REASONS=REASONS, is_admin=is_admin(), me=u, ic=ic, ava=safe_ava, ago=ago, unread=unread, path=request.path,
                csrf=lambda: Markup(f'<input type="hidden" name="_csrf" value="{session.setdefault("csrf", secrets.token_hex(16))}">'))


@app.before_request
def csrf_guard():
    if request.method == "POST" and request.path not in ("/api/quiz/finish",):
        if not secrets.compare_digest(request.form.get("_csrf", ""), session.get("csrf", "x")):
            abort(400)


RATE = {}
def throttle(key, limit, window):
    t = time.time()
    if len(RATE) > 4000: RATE.clear()
    RATE[key] = [x for x in RATE.get(key, []) if t - x < window]
    if len(RATE[key]) >= limit: return False
    RATE[key].append(t)
    return True


POSTQ = ("SELECT p.*,u.username,u.avatar,u.id uid_user,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) lc,"
         "(SELECT COUNT(*) FROM comments WHERE post_id=p.id) cc,"
         "(SELECT COUNT(*) FROM likes WHERE post_id=p.id AND user_id=?) liked FROM posts p JOIN users u ON u.id=p.user_id ")


def uid(): return me()["id"] if me() else 0


@app.route("/")
def index():
    tab = request.args.get("t", "decouverte")
    conds, args = ["p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?)"], [uid(), uid()]
    if tab == "suivis" and me():
        conds.append("p.mal_id IN (SELECT mal_id FROM favs WHERE user_id=?)")
        args.append(uid())
    posts = db().execute(POSTQ + "WHERE " + " AND ".join(conds) + " ORDER BY p.id DESC LIMIT 40", args).fetchall()
    watch = [r[0] for r in db().execute("SELECT mal_id FROM favs WHERE user_id=? AND status IN('cours','voir') AND kind='anime' ORDER BY updated DESC LIMIT 50", (uid(),))] if me() else []
    return render_template("home.html", title="Découverte", tab=tab, posts=posts, eta=next_tournament(), watch=watch)


@app.route("/recherche")
def search():
    qs = txt(request.args.get("q"), 60)
    us = db().execute("SELECT username,bio,avatar FROM users WHERE username LIKE ? AND privacy='public' LIMIT 12", (f"%{qs}%",)).fetchall() if qs else []
    return render_template("search.html", title="Recherche", q=qs, us=us)


@app.route("/publier", methods=["POST"])
@login_required
def publish():
    body = txt(request.form.get("body"), 1000)
    if not body or not throttle(f"pub{uid()}", 8, 300):
        flash("Message vide ou trop rapide.", "err")
        return redirect(request.referrer or "/")
    mid = request.form.get("mal_id", type=int)
    t = txt(request.form.get("m_title"), 120) if mid else None
    img = request.form.get("m_img", "") if mid else None
    if img and not img.startswith("https://cdn.myanimelist.net/"): img = ""
    db().execute("INSERT INTO posts(user_id,body,mal_id,m_title,m_img,created) VALUES(?,?,?,?,?,?)", (uid(), body, mid, t, img, now()))
    return redirect(request.referrer or "/")


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
        if not me(): return redirect("/connexion")
        b = txt(request.form.get("body"), 500)
        if b and throttle(f"c{uid()}", 10, 300):
            d.execute("INSERT INTO comments(post_id,user_id,body,created) VALUES(?,?,?,?)", (pid, uid(), b, now()))
            notify(p["user_id"], f"{me()['username']} a commenté ton post", f"/post/{pid}")
        return redirect(f"/post/{pid}")
    cm = d.execute("SELECT c.*,u.username,u.avatar FROM comments c JOIN users u ON u.id=c.user_id WHERE post_id=? AND c.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?) ORDER BY c.id", (pid, uid())).fetchall()
    return render_template("post.html", title="Post", p=p, cm=cm)


@app.route("/supprimer/<kind>/<int:i>", methods=["POST"])
@login_required
def delete(kind, i):
    tb = {"post": "posts", "comment": "comments", "cpost": "cposts"}.get(kind) or abort(404)
    r = db().execute(f"SELECT user_id FROM {tb} WHERE id=?", (i,)).fetchone() or abort(404)
    if r["user_id"] != uid(): abort(403)
    db().execute(f"DELETE FROM {tb} WHERE id=?", (i,))
    return redirect(request.referrer or "/")


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
    if a == "inc": ep += 1
    elif a == "dec": ep = max(0, ep - 1)
    elif f.get("ep") not in (None, ""): ep = toint(f.get("ep"), ep)
    if total and ep > total: ep = total
    if a in ("inc", "set") and total and ep >= total: st = "fini"
    elif a in ("inc", "set") and ep > 0 and st == "voir": st = "cours"
    score = toint(f.get("score"), cur["score"] if cur else 0, 0, 10)
    title = txt(f.get("title"), 120) or (cur["title"] if cur else "?")
    img = f.get("img") or (cur["img"] if cur else "")
    img = img if img.startswith("https://") else ""
    kind = "manga" if (f.get("kind") == "manga" or (cur and cur["kind"] == "manga")) else "anime"
    d.execute("INSERT OR REPLACE INTO favs(user_id,mal_id,title,img,status,ep,total,score,kind,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (uid(), mid, title, img, st, ep, total, score, kind, now()))
    if a == "set": flash("Ta liste est à jour.", "ok")
    return redirect(request.referrer or "/liste")


@app.route("/communautes", methods=["GET", "POST"])
def communities():
    d = db()
    if request.method == "POST":
        if not me(): return redirect("/connexion")
        n, tp, ds = txt(request.form.get("name"), 40), txt(request.form.get("topic"), 40), txt(request.form.get("descr"), 300)
        if len(n) < 3: flash("Nom trop court (3 min).", "err")
        elif d.execute("SELECT 1 FROM communities WHERE name=?", (n,)).fetchone(): flash("Ce nom existe déjà.", "err")
        else:
            cid = d.execute("INSERT INTO communities(name,topic,descr,creator_id,created) VALUES(?,?,?,?,?)", (n, tp, ds, uid(), now())).lastrowid
            d.execute("INSERT INTO members VALUES(?,?)", (cid, uid()))
            return redirect(f"/communaute/{cid}")
    rows = d.execute("SELECT c.*,(SELECT COUNT(*) FROM members WHERE cid=c.id) n,(SELECT COUNT(*) FROM members WHERE cid=c.id AND user_id=?) joined "
                     "FROM communities c ORDER BY n DESC,c.id DESC", (uid(),)).fetchall()
    return render_template("communities.html", title="Communautés", rows=rows)


@app.route("/communaute/<int:cid>", methods=["GET", "POST"])
def community(cid):
    d = db()
    c = d.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone() or abort(404)
    joined = me() and d.execute("SELECT 1 FROM members WHERE cid=? AND user_id=?", (cid, uid())).fetchone()
    if request.method == "POST":
        if not me(): return redirect("/connexion")
        a = request.form.get("action")
        if a == "join":
            if joined: d.execute("DELETE FROM members WHERE cid=? AND user_id=?", (cid, uid()))
            else: d.execute("INSERT INTO members VALUES(?,?)", (cid, uid()))
        elif a == "post" and joined and throttle(f"cp{uid()}", 8, 300):
            b = txt(request.form.get("body"), 800)
            if b: d.execute("INSERT INTO cposts(cid,user_id,body,created) VALUES(?,?,?,?)", (cid, uid(), b, now()))
        return redirect(f"/communaute/{cid}")
    posts = d.execute("SELECT p.*,u.username,u.avatar FROM cposts p JOIN users u ON u.id=p.user_id WHERE cid=? AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?) ORDER BY p.id DESC LIMIT 50", (cid, uid())).fetchall()
    mem = d.execute("SELECT u.username,u.avatar FROM members m JOIN users u ON u.id=m.user_id WHERE cid=? LIMIT 12", (cid,)).fetchall()
    return render_template("community.html", title=c["name"], c=c, joined=joined, posts=posts, mem=mem)


@app.route("/quiz")
def quiz():
    d = db()
    day = now()[:10]
    stats = d.execute("SELECT COUNT(*) n,COALESCE(SUM(good),0) g,COALESCE(SUM(total),0) t,COALESCE(MAX(good),0) b FROM scores WHERE user_id=?", (uid(),)).fetchone() if me() else None
    board = d.execute("SELECT u.username,u.avatar,MAX(s.good) g,MIN(s.ms) ms FROM scores s JOIN users u ON u.id=s.user_id WHERE s.day=? AND s.mode='tournoi' "
                      "GROUP BY u.id ORDER BY g DESC,ms ASC LIMIT 10", (day,)).fetchall()
    played = me() and d.execute("SELECT 1 FROM scores WHERE user_id=? AND day=? AND mode='tournoi'", (uid(), day)).fetchone()
    return render_template("quiz.html", title="Quiz", eta=next_tournament(), stats=stats, board=board, played=played, QC=Q_COUNT, QT=Q_TIME)


@app.route("/api/quiz/finish", methods=["POST"])
@login_required
def api_finish():
    j = request.get_json(silent=True) or {}
    mode = "tournoi" if j.get("mode") == "tournoi" else "solo"
    try: total, good, ms = int(j.get("total")), int(j.get("good")), int(j.get("ms"))
    except (TypeError, ValueError): return jsonify(error="invalide"), 400
    if not (1 <= total <= Q_COUNT and 0 <= good <= total) or not throttle(f"q{uid()}", 20, 3600): return jsonify(error="refusé"), 400
    d = db()
    if mode == "tournoi" and d.execute("SELECT 1 FROM scores WHERE user_id=? AND day=? AND mode='tournoi'", (uid(), now()[:10])).fetchone():
        return jsonify(error="Déjà joué aujourd'hui"), 403
    d.execute("INSERT INTO scores(user_id,mode,good,total,ms,day,created) VALUES(?,?,?,?,?,?,?)", (uid(), mode, good, total, max(ms, total * 1500), now()[:10], now()))
    return jsonify(ok=True)


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
    return render_template("profile.html", title=username, u=u, locked=False, posts=posts, favs=favs, st=st, own=own)


@app.route("/messages")
@login_required
def inbox():
    rows = db().execute("""SELECT u.username,u.avatar,m.body,m.created,(SELECT COUNT(*) FROM msgs WHERE s=u.id AND r=? AND read=0) un FROM msgs m
        JOIN users u ON u.id=CASE WHEN m.s=? THEN m.r ELSE m.s END
        WHERE m.id IN (SELECT MAX(id) FROM msgs WHERE s=? OR r=? GROUP BY CASE WHEN s=? THEN r ELSE s END) ORDER BY m.id DESC""",
                        (uid(),) + (uid(),) * 4).fetchall()
    return render_template("inbox.html", title="Messages", rows=rows)


@app.route("/messages/<username>", methods=["GET", "POST"])
@login_required
def thread(username):
    d = db()
    o = d.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone() or abort(404)
    if o["id"] == uid(): return redirect("/messages")
    if request.method == "POST":
        b = txt(request.form.get("body"), 1000)
        if d.execute("SELECT 1 FROM blocks WHERE (user_id=? AND blocked_id=?) OR (user_id=? AND blocked_id=?)", (uid(), o["id"], o["id"], uid())).fetchone():
            flash("Message impossible : un blocage existe.", "err")
        elif o["dm"] == "none": flash("Cette personne n'accepte pas les messages.", "err")
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


@app.route("/parametres", methods=["GET", "POST"])
@login_required
def settings():
    d, f, u = db(), request.form, me()
    if request.method == "POST":
        a = f.get("action")
        if a == "prefs":
            em = txt(f.get("email"), 120).lower()
            avatar = txt(f.get("avatar"), 500)
            if avatar and not avatar.startswith("https://"):
                flash("Le lien de la photo doit commencer par https://", "err")
                return redirect("/parametres")
            if em and not EMAIL_RE.fullmatch(em):
                flash("Adresse e-mail invalide.", "err")
                return redirect("/parametres")
            if em and d.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (em, u["id"])).fetchone():
                flash("Cet e-mail est déjà utilisé.", "err")
                return redirect("/parametres")
            d.execute("UPDATE users SET bio=?,avatar=?,email=?,theme=?,notify=?,privacy=?,dm=? WHERE id=?",
                      (txt(f.get("bio"), 300), avatar, em, "light" if f.get("theme") == "light" else "dark", int(bool(f.get("notify"))),
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
            if not throttle(f"pw{u['id']}", 5, 600): flash("Trop de tentatives.", "err")
            elif not check_password_hash(u["pw"], f.get("old", "")[:128]): flash("Mot de passe actuel incorrect.", "err")
            elif pw_problem(f.get("new", ""), u["username"]): flash(pw_problem(f.get("new", ""), u["username"]), "err")
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
    data = dict(profil=dict(username=me()["username"], bio=me()["bio"], avatar=me()["avatar"], created=me()["created"]), posts=q("SELECT body,created FROM posts WHERE user_id=?"),
                favoris=q("SELECT mal_id,title FROM favs WHERE user_id=?"), scores=q("SELECT mode,good,total,day FROM scores WHERE user_id=?"))
    return app.response_class(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                              headers={"Content-Disposition": "attachment; filename=senpaix-export.json"})


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def new_code():
    c = secrets.token_hex(6).upper()
    return "-".join(c[i:i + 4] for i in (0, 4, 8))


def norm_code(c): return re.sub(r"[^0-9A-Z]", "", (c or "").upper())


def find_user(ident):
    ident = (ident or "").strip()[:120]
    return db().execute("SELECT * FROM users WHERE username=? OR (email!='' AND email=?)", (ident, ident.lower())).fetchone()


@app.route("/api/pseudo")
def api_pseudo():
    n = request.args.get("u", "").strip()
    if not throttle("ps" + str(request.remote_addr), 40, 60): return jsonify(ok=False, msg="Trop de vérifications"), 429
    if not re.fullmatch(r"[\w.\-]{3,20}", n): return jsonify(ok=False, msg="3-20 caractères : lettres, chiffres, . _ -")
    if db().execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (n,)).fetchone(): return jsonify(ok=False, msg="Pseudo déjà pris")
    return jsonify(ok=True, msg="Pseudo disponible")


@app.route("/inscription", methods=["GET", "POST"])
def register():
    f = request.form
    if request.method == "POST":
        n, em, p = txt(f.get("username"), 20), txt(f.get("email"), 120).lower(), f.get("password", "")
        d = db()
        if not re.fullmatch(r"[\w.\-]{3,20}", n): flash("Pseudo : 3-20 caractères.", "err")
        elif em and not EMAIL_RE.fullmatch(em): flash("Adresse e-mail invalide.", "err")
        elif pw_problem(p, n): flash(pw_problem(p, n), "err")
        elif p != f.get("password2"): flash("Les mots de passe diffèrent.", "err")
        elif not (f.get("age") and f.get("cgu")): flash("Conditions requises.", "err")
        elif not throttle("reg" + str(request.remote_addr), 5, 3600): flash("Trop d'inscriptions.", "err")
        elif d.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (n,)).fetchone(): flash("Pseudo déjà pris.", "err")
        elif em and d.execute("SELECT 1 FROM users WHERE email=?", (em,)).fetchone(): flash("E-mail déjà utilisé.", "err")
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
    if not code: return redirect("/")
    return render_template("welcome.html", title="Bienvenue", code=code, bare=True)


DUMMY = generate_password_hash("x" + secrets.token_hex(6))


@app.route("/connexion", methods=["GET", "POST"])
def login():
    f = request.form
    if request.method == "POST":
        ident, p = f.get("username", "").strip(), f.get("password", "")[:128]
        if not throttle("log" + str(request.remote_addr) + ident.lower(), 6, 600) or not throttle("acct" + ident.lower(), 20, 3600):
            flash("Trop de tentatives.", "err")
        else:
            u = find_user(ident)
            if u and u["banned"]: flash("Compte suspendu.", "err")
            elif check_password_hash(u["pw"] if u else DUMMY, p) and u:
                session.clear(); session.update(uid=u["id"], sv=u["sv"]); session.permanent = bool(f.get("remember"))
                nx = request.args.get("next", "/")
                return redirect(nx if nx.startswith("/") and not nx.startswith("//") else "/")
            flash("Identifiant ou mot de passe incorrect.", "err")
    return render_template("auth.html", title="Connexion", reg=False, bare=True, v=f)


@app.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def forgot():
    f = request.form
    if request.method == "POST":
        u = find_user(f.get("username"))
        new = f.get("new", "")
        if not throttle("fg" + str(request.remote_addr), 5, 900): flash("Trop de tentatives.", "err")
        elif pw_problem(new, (u["username"] if u else "")) or new != f.get("new2"): flash("Mots de passe invalides.", "err")
        elif not u or not u["recovery"] or not check_password_hash(u["recovery"], norm_code(f.get("code"))): flash("Code incorrect.", "err")
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
    if kind not in ("post", "comment", "cpost", "user"): abort(404)
    r = request.form.get("reason")
    r = r if r in REASONS else "Autre"
    d = db()
    if not throttle(f"rp{uid()}", 10, 3600): flash("Trop de signalements.", "err")
    elif d.execute("SELECT 1 FROM reports WHERE reporter_id=? AND kind=? AND target_id=? AND status='ouvert'", (uid(), kind, i)).fetchone():
        flash("Déjà signalé.", "ok")
    else:
        d.execute("INSERT INTO reports(reporter_id,kind,target_id,reason,created) VALUES(?,?,?,?,?)", (uid(), kind, i, r, now()))
        flash("Signalement envoyé.", "ok")
    return redirect(request.referrer or "/")


@app.route("/bloquer/<username>", methods=["POST"])
@login_required
def block(username):
    d = db()
    o = d.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone() or abort(404)
    if o["id"] == uid(): flash("Impossible de se bloquer soi-même.", "err")
    elif d.execute("DELETE FROM blocks WHERE user_id=? AND blocked_id=?", (uid(), o["id"])).rowcount: flash("Débloqué.", "ok")
    else:
        d.execute("INSERT INTO blocks VALUES(?,?)", (uid(), o["id"]))
        flash("Bloqué.", "ok")
    return redirect(request.referrer or "/")


@app.route("/bloques")
@login_required
def blocked():
    rows = db().execute("SELECT u.username,u.avatar FROM blocks b JOIN users u ON u.id=b.blocked_id WHERE b.user_id=?", (uid(),)).fetchall()
    return render_template("blocked.html", title="Bloqués", rows=rows)


def target_info(kind, i):
    d = db()
    if kind == "user": r = d.execute("SELECT id uid,username,bio body FROM users WHERE id=?", (i,)).fetchone()
    else:
        tb = {"post": "posts", "comment": "comments", "cpost": "cposts"}[kind]
        r = d.execute(f"SELECT t.user_id uid,u.username,t.body FROM {tb} t JOIN users u ON u.id=t.user_id WHERE t.id=?", (i,)).fetchone()
    return r


def delete_content(kind, i):
    tb = {"post": "posts", "comment": "comments", "cpost": "cposts"}.get(kind)
    if tb: db().execute("DELETE FROM %s WHERE id=?" % tb, (i,))
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
    log_rows = d.execute("SELECT l.*,u.username adm FROM admin_log l JOIN users u ON u.id=l.admin_id ORDER BY l.id DESC LIMIT 15").fetchall()
    return render_template("admin.html", title="Modération", tab="reports", stats=stats, rows=rows, log=log_rows)


@app.route("/admin/membres")
@admin_only
def admin_members():
    q = txt(request.args.get("q"), 40)
    rows = db().execute("SELECT u.id,u.username,u.email,u.avatar,u.created,u.banned,u.is_admin,(SELECT COUNT(*) FROM posts WHERE user_id=u.id) np,"
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
    get = lambda tb: d.execute(f"SELECT t.id,t.body,t.created,u.id uid,u.username FROM {tb} t JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 30").fetchall()
    return render_template("admin_content.html", title="Contenus", tab="content", posts=get("posts"), comments=get("comments"), cposts=get("cposts"))


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
                alog("supprime", f"{r['kind']} #{r['target_id']}", f"auteur {t['username']}")
            elif act == "rban" and t: ban_user(t["uid"], r["reason"])
            d.execute("UPDATE reports SET status=? WHERE id=?", ("rejeté" if act == "dismiss" else "traité", r["id"]))
            flash("Signalement traité.", "ok")
    elif act == "ban": ban_user(i, txt(f.get("reason"), 120))
    elif act == "unban":
        u = d.execute("SELECT username FROM users WHERE id=?", (i,)).fetchone()
        if u:
            d.execute("UPDATE users SET banned=0,ban_reason='' WHERE id=?", (i,))
            flash("Compte rétabli.", "ok")
    elif act == "delete_user":
        u = d.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone()
        if u and u["id"] != uid() and not user_is_admin(u) and f.get("confirm") == u["username"]:
            d.execute("DELETE FROM users WHERE id=?", (i,))
            flash("Compte supprimé.", "ok")
            return redirect("/admin/membres")
        flash("Confirmation incorrecte.", "err")
    elif act == "delete_content":
        kind = f.get("kind")
        if delete_content(kind, i): flash("Contenu supprimé.", "ok")
    return redirect(request.referrer or "/admin")


def ban_user(i, reason=""):
    d = db()
    u = d.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone()
    if not u or u["id"] == uid() or user_is_admin(u): return
    d.execute("UPDATE users SET banned=1,ban_reason=?,sv=sv+1 WHERE id=?", (txt(reason, 120), i))
    flash("Suspendu.", "ok")


@app.route("/admin/export")
@admin_only
def admin_export():
    d, out = db(), {}
    for tb, cols in (("users", "id,username,email,avatar,bio,created,banned,is_admin"), ("posts", "*"), ("comments", "*"), ("communities", "*"), ("members", "*"), ("cposts", "*"),
                     ("favs", "*"), ("scores", "*"), ("reports", "*"), ("admin_log", "*")):
        out[tb] = [rowd(r) for r in d.execute(f"SELECT {cols} FROM {tb}").fetchall()]
    return app.response_class(json.dumps(out, ensure_ascii=False, indent=1, default=str), mimetype="application/json",
                              headers={"Content-Disposition": f"attachment; filename=senpaix-sauvegarde-{now()[:10]}.json"})


@app.route("/healthz")
def healthz():
    if request.args.get("db"): db().execute("SELECT 1").fetchone()
    return "ok"


@app.route("/robots.txt")
def robots(): return app.response_class("User-agent: *\nDisallow: /admin\nDisallow: /api/\nDisallow: /messages\n", mimetype="text/plain")


SW = r"""const V='sx-v1';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==V).map(x=>caches.delete(x)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);
 if(u.origin===location.origin){if(u.pathname.startsWith('/api/')||u.pathname==='/sw.js')return;
  e.respondWith(fetch(r).then(x=>{if(x.ok){const c=x.clone();caches.open(V).then(h=>h.put(r,c))}return x}).catch(()=>caches.match(r).then(m=>m||caches.match('/')||new Response('Hors connexion',{status:503}))))}});
"""


@app.route("/sw.js")
def sw(): return app.response_class(SW, mimetype="application/javascript")


@app.route("/diagnostic")
def diagnostic(): return render_template("diag.html", title="Diagnostic")


@app.route("/confidentialite")
def privacy_page(): return render_template("priv.html", title="Confidentialité", creator=CREATOR, email=EMAIL)


@app.route("/conditions")
def terms_page(): return render_template("cgu.html", title="Conditions", creator=CREATOR, email=EMAIL)


@app.route("/a-propos")
def about(): return render_template("about.html", title="À propos", creator=CREATOR, bio=CREATOR_BIO, wa=WA, wa_show=WA_SHOW, email=EMAIL, fb=FB)


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
       "connect-src 'self' https://graphql.anilist.co https://api.jikan.moe; manifest-src 'self'; worker-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def secure(r):
    h = r.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if request.path != "/sw.js": h["Content-Security-Policy"] = CSP
    if PROD: h["Strict-Transport-Security"] = "max-age=31536000"
    if r.mimetype == "text/html": h["Cache-Control"] = "private, no-cache"
    return r


@app.errorhandler(500)
def err500(e): return render_template("err.html", title="Erreur", code=500), 500


@app.errorhandler(404)
@app.errorhandler(403)
@app.errorhandler(400)
def err(e): return render_template("err.html", title="Oups", code=e.code), e.code


# Style CSS mis à jour avec les animations néon et avatars personnalisés
CSS = r"""
:root{--bg:#070b1a;--bg2:#0d1428;--card:#121a30;--card2:#18213c;--line:#222c4a;--text:#f3f5ff;--mut:#8f9bbd;--or:#ff6a1a;--or2:#ff9a3c;--red:#e5233b;--r:18px}
html[data-theme=light]{--bg:#f4f5fa;--bg2:#fff;--card:#fff;--card2:#eef0f7;--line:#dde0ee;--text:#14182b;--mut:#626b8a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:radial-gradient(120% 50% at 50% 0,#2a1608 0,transparent 55%) no-repeat,var(--bg);color:var(--text);font:15px/1.45 "Segoe UI",system-ui,sans-serif;padding-bottom:84px}
a{color:inherit;text-decoration:none}.mut{color:var(--mut)}.ic{flex:none;vertical-align:middle}
header{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(14px);padding-top:env(safe-area-inset-top)}
.top{max-width:640px;margin:auto;display:flex;align-items:center;gap:14px;padding:10px 16px}
.top .sp{flex:1}.top .b{position:relative;display:flex}.top .b i{position:absolute;top:-6px;right:-8px;background:var(--red);color:#fff;border-radius:99px;font:700 .62rem/1 sans-serif;padding:3px 5px;font-style:normal}
.menu{position:relative}.menu summary{list-style:none;cursor:pointer;display:flex;padding:2px}.menu summary::-webkit-details-marker{display:none}
.mn{position:absolute;right:0;top:36px;min-width:210px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:6px;box-shadow:0 12px 40px #000a}
.mn a,.mn button{display:flex;align-items:center;gap:12px;width:100%;padding:12px;border-radius:12px;background:none;color:var(--text);font:600 .92rem inherit;font-family:inherit;justify-content:flex-start}
.logo{font:900 1.25rem/1 "Trebuchet MS",sans-serif;letter-spacing:-.5px}.logo b{color:var(--or)}
main{max-width:640px;margin:auto;padding:4px 16px}
h1{font-size:1.5rem;margin:6px 0 12px;font-weight:800}h2{font-size:1.02rem;margin:20px 0 10px;font-weight:800}
.hero{font:900 2.1rem/1.02 "Trebuchet MS",sans-serif;text-align:center;margin:12px 0 16px}.hero em{font-style:normal;color:var(--or);display:block;font-size:2.6rem}
.card{background:linear-gradient(160deg,var(--card),var(--bg2));border:1px solid var(--line);border-radius:var(--r);padding:14px;margin:10px 0}
.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;border-radius:99px;padding:11px 20px;background:linear-gradient(100deg,var(--or),var(--or2));color:#fff;font:700 .92rem inherit;font-family:inherit;cursor:pointer}
.btn:active,button:active{transform:scale(.97)}.btn.g{background:transparent;border:1.5px solid var(--line);color:var(--text)}.btn.s{padding:7px 14px;font-size:.8rem}.btn.w{width:100%}.btn.r{background:#7a1c2e}
input,textarea,select{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:var(--bg2);color:var(--text);font:inherit}
textarea{min-height:84px;resize:vertical}label{display:block;margin:12px 0 5px;color:var(--mut);font-size:.8rem;font-weight:600}

/* Styles et animations des avatars personnalisés */
.ava{display:inline-flex;align-items:center;justify-content:center;border-radius:50%;color:#fff;font-weight:800;flex:none}
.ava-img{border-radius:50%;object-fit:cover;display:inline-block;box-shadow:0 0 10px rgba(255, 106, 26, 0.4);transition:transform .3s ease,box-shadow .3s ease;border:2px solid var(--or);flex:none}
.ava-img:hover{transform:scale(1.08) rotate(3deg);box-shadow:0 0 18px rgba(255, 106, 26, .8),0 0 30px rgba(255, 154, 60, .5)}
.profile-avatar-glow{position:relative;display:inline-block;padding:4px;border-radius:50%;background:linear-gradient(45deg,var(--or),#ff3d71,var(--or2),#9b51e0);background-size:300% 300%;animation:neonGlow 4s ease infinite}
@keyframes neonGlow{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}

.stories{display:flex;gap:12px;overflow-x:auto;padding:4px 0 8px;scrollbar-width:none}.stories::-webkit-scrollbar{display:none}
.st{flex:none;width:74px;text-align:center;font-size:.68rem}.st .ring{width:72px;height:72px;border-radius:50%;padding:2.5px;background:conic-gradient(var(--or),#ff3d71,var(--or2),var(--or))}
.st img{width:100%;height:100%;border-radius:50%;object-fit:cover;border:2.5px solid var(--bg);display:block;background:var(--card)}
.st span{display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pills{display:flex;gap:8px;overflow-x:auto;margin:6px 0 10px;scrollbar-width:none}
.pill{flex:none;padding:9px 16px;border-radius:99px;background:var(--card2);font-size:.82rem;font-weight:700;display:flex;align-items:center;gap:6px;color:var(--mut)}.pill.on{background:#fff;color:#111}
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
.it small{display:block;color:var(--mut);font-size:.74rem;font-weight:400}.it .grow{font-size:.95rem}
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
"""

POST_MACRO = r"""{% macro flag(kind, id, username) %}{% if me and me.username!=username %}<details class="more"><summary>{{ ic('dots',18) }}</summary><div class="mn" style="top:26px;right:0;min-width:220px"><form method="post" action="/signaler/{{ kind }}/{{ id }}">{{ csrf() }}<select name="reason" style="margin-bottom:6px">{% for r in REASONS %}<option>{{ r }}</option>{% endfor %}</select><button>{{ ic('shield',18) }} Signaler</button></form><form method="post" action="/bloquer/{{ username }}">{{ csrf() }}<button>{{ ic('lock',18) }} Bloquer {{ username }}</button></form></div></details>{% endif %}{% endmacro %}
{% macro post(p, full=False) %}<article class="post"><div class="ph">{{ ava(p.username,38) }}<div class="grow"><a href="/profil/{{ p.username }}"><b>{{ p.username }}</b></a><small>{{ ago(p.created) }}</small></div>
{% if me and me.id==p.user_id %}<form method="post" action="/supprimer/post/{{ p.id }}" onsubmit="return confirm('Supprimer ?')">{{ csrf() }}<button class="btn g s">{{ ic('trash',16) }}</button></form>{% else %}{{ flag('post',p.id,p.username) }}{% endif %}</div>
<p>{{ p.body }}</p>
{% if p.mal_id %}<a class="att" href="/serie/{{ p.mal_id }}">{% if p.m_img %}<img src="{{ p.m_img }}" loading="lazy" alt="">{% endif %}<div><b>{{ p.m_title }}</b><small class="mut" style="display:block">Voir la fiche →</small></div></a>{% endif %}
<div class="act"><form method="post" action="/aimer/{{ p.id }}">{{ csrf() }}<button class="{{ 'on' if p.liked }}">{{ ic('heart',19) }} {{ p.lc }}</button></form><a href="/post/{{ p.id }}">{{ ic('msg',19) }} {{ p.cc }}</a></div></article>{% endmacro %}
{% macro tiles(data) %}<div class="grid">{% for m in data %}<a class="tile" href="/serie/{{ m.id }}"><img src="{{ m.img }}" loading="lazy" alt="">{% if m.score %}<small>★ {{ m.score }}</small>{% endif %}<b>{{ m.title }}</b></a>{% endfor %}</div>{% if not data %}<p class="mut" style="text-align:center">Aucun résultat.</p>{% endif %}{% endmacro %}
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
{% if me %}<a href="/liste">{{ ic('star',18) }} Ma liste</a>{% endif %}<a href="/catalogue">{{ ic('compass',18) }} Catalogue</a>{% if is_admin %}<a href="/admin">{{ ic('shield',18) }} Modération</a>{% endif %}<a href="/a-propos">{{ ic('info',18) }} À propos</a><a href="/confidentialite">{{ ic('lock',18) }} Confidentialité</a><a href="/conditions">{{ ic('shield',18) }} Conditions</a>{% if me %}<form method="post" action="/deconnexion">{{ csrf() }}<button>{{ ic('out',18) }} Se déconnecter</button></form>{% endif %}</div></details></div></header>{% endif %}
<main>{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% block c %}{% endblock %}</main>
{% if not bare %}<nav class="tabs"><div>{% for h,i,l in [('/','home','Découverte'),('/communautes','users','Groupes'),('/quiz','game','Quiz'),('/catalogue','compass','Catalogue'),('/profil/'+me.username if me else '/connexion','user','Profil')] %}
<a href="{{ h }}" class="{{ 'on' if (path==h or (h!='/' and path.startswith(h[:6]))) }} {{ 'mid' if i=='game' }}">{{ ic(i) }}{{ l }}</a>{% endfor %}</div></nav>{% endif %}</body></html>"""

T["home.html"] = r"""{% extends "base.html" %}{% from "m.html" import post, cd with context %}{% block c %}
<div class="hero">L'univers<em>Otaku</em>autrement.</div>
<div class="stories" id="stories"></div>
<div class="pills"><a class="pill {{ 'on' if tab=='decouverte' }}" href="/">{{ ic('compass',16) }} Découverte</a><a class="pill {{ 'on' if tab=='suivis' }}" href="/?t=suivis">{{ ic('star',16) }} Mes séries</a><a class="pill" href="/catalogue?t=planning">{{ ic('home',16) }} Planning</a><a class="pill" href="/quiz">{{ ic('game',16) }} Quiz</a></div>
<a class="promo" href="/quiz"><span class="lg">TKN</span><span><b>Le prochain tournoi solo arrive bientôt</b><small>Démarrage dans {{ (eta//3600) }}h {{ (eta%3600//60) }}m</small></span></a>
{% if me %}<form method="post" action="/publier" class="card">{{ csrf() }}<div class="row">{{ ava(me.username,36) }}<textarea name="body" maxlength="1000" placeholder="Partage ton avis, un spoiler…" style="min-height:52px"></textarea></div><div style="text-align:right;margin-top:8px"><button class="btn s">Publier</button></div></form>
{% else %}<div class="card" style="text-align:center">Rejoins la communauté.<br><a class="btn s" style="margin-top:8px" href="/inscription">Créer un compte</a></div>{% endif %}
<h2>Fil de la communauté</h2>{% for p in posts %}{{ post(p) }}{% else %}<p class="mut" style="text-align:center;margin-top:24px">Aucun post pour le moment.</p>{% endfor %}
<h2>À l'affiche cette saison</h2><div class="stories" id="airing"></div>
<script>$('airing').innerHTML=skC(5);L('saison').then(function(l){$('airing').innerHTML=posters(l.slice(0,12))}).catch(function(){});</script>{% endblock %}"""

T["search.html"] = r"""{% extends "base.html" %}{% block c %}
<form><div class="row"><input name="q" id="q" value="{{ q }}" placeholder="Animé, manga, membre…" autocomplete="off" autofocus><button class="btn">{{ ic('search',18) }}</button></div></form>
{% if us %}<h2>Membres</h2><div class="grp">{% for u in us %}<a class="it" href="/profil/{{ u.username }}">{{ ava(u.username,36) }}<span class="grow">{{ u.username }}<small>{{ u.bio[:60] }}</small></span></a>{% endfor %}</div>{% endif %}
<div id="res"></div>
<script>var tm;function run(q){q=q.trim();if(q.length<2){$('res').innerHTML='';return}$('res').innerHTML=skT(6);
 L('search',{q:q}).then(function(l){$('res').innerHTML=l.length?'<h2>Animés</h2>'+tiles(l.slice(0,18)):'<p class="mut" style="text-align:center">Aucun résultat.</p>'}).catch(function(){})}
$('q').oninput=function(){clearTimeout(tm);var v=this.value;tm=setTimeout(function(){run(v)},450)};</script>{% endblock %}"""

T["catalog.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Catalogue</h1>
<div class="pills">{% for k,l in [('saison','En cours'),('explore','Explorer'),('top','Top'),('planning','Planning'),('manga','Mangas')] %}<a class="pill {{ 'on' if t==k }}" href="/catalogue?t={{ k }}">{{ l }}</a>{% endfor %}</div>
<div id="res" class="grid"></div><div id="mo" class="mut" style="text-align:center;padding:18px">Chargement…</div>
<script>var K='{{ t }}',pg=1,busy=0,done=0,seen={},G=$('res'),O={day:'{{ day }}',genre:'',year:'',sort:'{{ so }}',min:''};
G.innerHTML=skI(9);
function more(){if(busy||done)return;busy=1;O.page=pg;L(K,O).then(function(l){if(pg==1)G.innerHTML='';l.forEach(function(m){if(!seen[m.id]){seen[m.id]=1;G.insertAdjacentHTML('beforeend',tileItems([m],K))}});if(l.length<8)done=1;pg++;busy=0;if(done)$('mo').hidden=true}).catch(function(){done=1;busy=0})}
if('IntersectionObserver' in window)new IntersectionObserver(function(e){if(e[0].isIntersecting)more()},{rootMargin:'500px'}).observe($('mo'));more();</script>{% endblock %}"""

T["media.html"] = r"""{% extends "base.html" %}{% from "m.html" import post with context %}{% block c %}
<div id="info"><div class="sk" style="height:190px"></div></div>
{% if me %}<form method="post" action="/liste/{{ mid }}" class="card">{{ csrf() }}<input type="hidden" name="title" id="ft"><input type="hidden" name="img" id="fi"><input type="hidden" name="total" id="fe"><input type="hidden" name="kind" value="{{ k }}">
<div class="row"><select name="status">{% for key,l in STATUSES.items() %}<option value="{{ key }}" {{ 'selected' if cur and cur.status==key }}>{{ l }}</option>{% endfor %}</select><select name="score"><option value="0">Ma note</option>{% for i in range(10,0,-1) %}<option value="{{ i }}" {{ 'selected' if cur and cur.score==i }}>★ {{ i }}/10</option>{% endfor %}</select></div>
<div class="row" style="margin-top:8px"><input type="number" name="ep" min="0" value="{{ cur.ep if cur else 0 }}" style="max-width:110px"><button class="btn grow">{{ ic('star',18) }} {{ 'Mettre à jour' if cur else 'Ajouter' }}</button></div></form>{% endif %}
<div class="card"><p id="syn" style="margin:0;white-space:pre-wrap" class="mut">Chargement…</p></div>
<h2>En parler</h2>{% for p in posts %}{{ post(p) }}{% endfor %}
<script>M({{ mid }},'{{ k }}').then(function(m){document.title=m.title+' · SenpaiX';
$('info').innerHTML='<div class="row" style="align-items:flex-start"><img src="'+esc(m.img)+'" alt="" style="width:130px;border-radius:16px"><div class="grow"><h1 style="font-size:1.2rem">'+esc(m.title)+'</h1><p class="mut">'+esc(m.type||'')+' · '+(m.year||'?')+'</p></div></div>';
$('syn').textContent=m.syn||'Pas de synopsis.';if($('ft')){$('ft').value=m.title;$('fi').value=m.img;$('fe').value=m.eps||0}}).catch(function(){});</script>{% endblock %}"""

T["post.html"] = r"""{% extends "base.html" %}{% from "m.html" import post with context %}{% block c %}{{ post(p) }}<h2>Commentaires</h2>
{% for c in cm %}<div class="post"><div class="ph">{{ ava(c.username,30) }}<div class="grow"><b>{{ c.username }}</b><small>{{ ago(c.created) }}</small></div></div><p>{{ c.body }}</p></div>{% endfor %}
{% if me %}<form method="post" class="row" style="margin-top:12px">{{ csrf() }}<input name="body" maxlength="500" placeholder="Commenter…" required><button class="btn">{{ ic('send',18) }}</button></form>{% endif %}{% endblock %}"""

T["communities.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Groupes</h1>
{% for c in rows %}<a class="card row" href="/communaute/{{ c.id }}"><span class="ava" style="width:46px;height:46px;background:var(--or)">{{ c.name[:1]|upper }}</span><div class="grow"><b>{{ c.name }}</b><small class="mut">{{ c.topic }} · {{ c.n }} membres</small></div></a>{% endfor %}
{% if me %}<h2>Créer un groupe</h2><form method="post" class="card">{{ csrf() }}<label>Nom</label><input name="name" maxlength="40" required><label>Thème</label><input name="topic" maxlength="40"><label>Description</label><textarea name="descr" maxlength="300"></textarea><p><button class="btn w">Créer</button></p></form>{% endif %}{% endblock %}"""

T["community.html"] = r"""{% extends "base.html" %}{% block c %}<h1>{{ c.name }}</h1><p class="mut">{{ c.topic }} — {{ c.descr }}</p>
{% if me %}<form method="post">{{ csrf() }}<input type="hidden" name="action" value="join"><button class="btn {{ 'g' if joined }} w">{{ 'Quitter' if joined else 'Rejoindre' }}</button></form>{% endif %}
{% if joined %}<form method="post" class="card">{{ csrf() }}<input type="hidden" name="action" value="post"><textarea name="body" maxlength="800" placeholder="Message au groupe…"></textarea><div style="text-align:right;margin-top:8px"><button class="btn s">Envoyer</button></div></form>{% endif %}
{% for p in posts %}<div class="post"><div class="ph">{{ ava(p.username,34) }}<div class="grow"><b>{{ p.username }}</b><small>{{ ago(p.created) }}</small></div></div><p>{{ p.body }}</p></div>{% endfor %}{% endblock %}"""

T["quiz.html"] = r"""{% extends "base.html" %}{% from "m.html" import cd with context %}{% block c %}
<h1>Tournoi</h1><div class="card" style="text-align:center"><small class="mut">Début dans :</small>{{ cd(eta) }}
{% if me %}<button class="btn w" onclick="play('tournoi')" {{ 'disabled style=opacity:.5' if played }}>{{ 'Déjà joué' if played else 'Jouer au tournoi' }}</button>{% endif %}</div>
<h2>Classement</h2><div class="grp">{% for b in board %}<div class="it">{{ ava(b.username,30) }}<span class="grow">{{ b.username }}</span><b>{{ b.g }}/{{ QC }}</b></div>{% else %}<div class="it mut">Aucun score.</div>{% endfor %}</div>{% endblock %}"""

T["profile.html"] = r"""{% extends "base.html" %}{% from "m.html" import post with context %}{% block c %}
<div style="text-align:center;margin-top:8px">
<div class="profile-avatar-glow">{{ ava(u.username,84) }}</div>
<h1 style="margin:10px 0 2px">{{ u.username }}</h1><small class="mut">Inscrit le {{ u.created[:10] }}</small>{% if not locked %}<p>{{ u.bio }}</p>{% endif %}
{% if own %}<a class="btn s g" href="/parametres">Modifier mon profil</a>{% endif %}</div>
{% if not locked %}<h2>Publications</h2>{% for p in posts %}{{ post(p) }}{% endfor %}{% endif %}{% endblock %}"""

T["inbox.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Messages</h1><div class="grp">{% for r in rows %}<a class="it" href="/messages/{{ r.username }}">{{ ava(r.username,40) }}<span class="grow"><b>{{ r.username }}</b><small>{{ r.body[:50] }}</small></span></a>{% endfor %}</div>{% endblock %}"""

T["thread.html"] = r"""{% extends "base.html" %}{% set back='/messages' %}{% block c %}<h1>{{ o.username }}</h1>{% for m in ms %}<div class="bubble {{ 'me' if m.s==me.id }}">{{ m.body }}</div>{% endfor %}
<form method="post" class="row" style="margin-top:12px">{{ csrf() }}<input name="body" maxlength="1000" placeholder="Message…" required autofocus><button class="btn">{{ ic('send',18) }}</button></form>{% endblock %}"""

T["notifs.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Notifications</h1>
<div class="grp">{% for n in rows %}<a class="it" href="{{ n.link }}"><span class="grow" style="{{ '' if n.read else 'font-weight:700' }}">{{ n.text }}<small>{{ ago(n.created) }}</small></span></a>{% endfor %}</div>{% endblock %}"""

# Template Paramètres avec le champ de personnalisation de la photo de profil
T["settings.html"] = r"""{% extends "base.html" %}{% from "m.html" import item, sw with context %}{% block c %}<h1>Paramètres</h1>
<div class="card row">{{ ava(me.username,56) }}<div class="grow"><b style="font-size:1.1rem">{{ me.username }}</b></div></div>
<form method="post">{{ csrf() }}<input type="hidden" name="action" value="prefs">
<div class="sec">Profil & Avatar</div>
<div class="grp" style="padding:12px 16px">
<label style="margin-top:0">Photo de profil (Lien d'une image en https://)</label>
<input type="url" name="avatar" value="{{ me.avatar or '' }}" placeholder="https://exemple.com/mon-avatar.jpg">
<label>Biographie</label><textarea name="bio" maxlength="300">{{ me.bio }}</textarea>
<label>E-mail</label><input type="email" name="email" maxlength="120" value="{{ me.email or '' }}"></div>
<div class="sec">Apparence</div><div class="grp">{{ sw('theme', me.theme=='light', 'Thème clair', '', 'compass') }}</div>
<p><button class="btn w">Enregistrer</button></p></form>
<div class="sec">Sécurité</div><div class="grp" style="padding:12px 16px"><form method="post">{{ csrf() }}<input type="hidden" name="action" value="password"><label style="margin-top:0">Ancien mot de passe</label><input type="password" name="old"><label>Nouveau (8+)</label><input type="password" name="new"><p><button class="btn s">Changer</button></p></form></div>
<div class="grp"><form method="post" action="/deconnexion">{{ csrf() }}<button class="it"><span class="ico">{{ ic('out',19) }}</span><span class="grow">Se déconnecter</span></button></form></div>{% endblock %}"""

T["auth.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><div class="logo" style="font-size:2.6rem">Senpai<b>X</b></div>
<div class="seg"><a class="{{ '' if reg else 'on' }}" href="/connexion">Connexion</a><a class="{{ 'on' if reg }}" href="/inscription">Inscription</a></div>
<form method="post" class="card">{{ csrf() }}
{% if reg %}<label>Pseudo</label><input name="username" required minlength="3" maxlength="20"><label>Mot de passe</label><input type="password" name="password" required minlength="8"><label>Confirmer</label><input type="password" name="password2" required>
<label class="chk"><input type="checkbox" name="age" required> 13 ans ou plus</label><label class="chk"><input type="checkbox" name="cgu" required> J'accepte les conditions</label><p><button class="btn w">S'inscrire</button></p>
{% else %}<label>Pseudo ou e-mail</label><input name="username" required><label>Mot de passe</label><input type="password" name="password" required><p><button class="btn w">Se connecter</button></p>{% endif %}</form></div>{% endblock %}"""

T["about.html"] = r"""{% extends "base.html" %}{% block c %}<h1>À propos</h1><div class="card"><p>{{ bio }}</p></div>{% endblock %}"""
T["diag.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Diagnostic</h1><p class="mut">OK</p>{% endblock %}"""
T["priv.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Confidentialité</h1><p class="mut">Données protégées.</p>{% endblock %}"""
T["cgu.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Conditions</h1><p class="mut">Règles de la communauté.</p>{% endblock %}"""
T["mylist.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Ma liste</h1>{% for r in rows %}<div class="card row"><b>{{ r.title }}</b></div>{% endfor %}{% endblock %}"""
T["blocked.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Bloqués</h1>{% endblock %}"""
T["adm.html"] = r"""{% extends "base.html" %}{% block c %}<h1>Modération</h1>{% block a %}{% endblock %}{% endblock %}"""
T["admin.html"] = r"""{% extends "adm.html" %}{% block a %}<p class="mut">Panneau admin actif.</p>{% endblock %}"""
T["admin_members.html"] = r"""{% extends "adm.html" %}{% block a %}{% endblock %}"""
T["admin_user.html"] = r"""{% extends "adm.html" %}{% block a %}{% endblock %}"""
T["admin_content.html"] = r"""{% extends "adm.html" %}{% block a %}{% endblock %}"""
T["err.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><h1 style="color:var(--or)">{{ code }}</h1><p class="mut">Erreur.</p><a class="btn" href="/">Accueil</a></div>{% endblock %}"""
T["welcome.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><h1>Bienvenue !</h1><div class="card"><code>{{ code }}</code></div><a class="btn w" href="/">Continuer</a></div>{% endblock %}"""
T["forgot.html"] = r"""{% extends "base.html" %}{% block c %}<div class="auth"><h1>Récupération</h1></div>{% endblock %}"""

app.jinja_loader = DictLoader(T)
app.jinja_env.globals["css"] = CSS
app.jinja_env.globals["js"] = '\nfunction $(x){return document.getElementById(x)}\nfunction esc(s){return String(s==null?\'\':s).replace(/[&<>"\']/g,function(c){return{\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]})}\nvar JK=\'https://api.jikan.moe/v4\',DAYS=[\'monday\',\'tuesday\',\'wednesday\',\'thursday\',\'friday\',\'saturday\',\'sunday\'];\nfunction req(u,o){var ac=new AbortController(),t=setTimeout(function(){ac.abort()},9000);o=o||{};o.signal=ac.signal;return fetch(u,o).then(function(r){if(!r.ok)throw new Error();return r.json()})}\nfunction get(u){return req(u)}\nfunction AL(q,v){return req(\'https://graphql.anilist.co\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({query:q,variables:v||{}})}).then(function(d){if(d.errors)throw new Error();return d.data})}\nvar MF=\'idMal isAdult title{romaji english} coverImage{large} averageScore seasonYear startDate{year} episodes chapters format genres description(asHtml:false)\';\nfunction alm(m){return{id:m.idMal,title:(m.title&&(m.title.english||m.title.romaji))||\'?\',img:(m.coverImage&&m.coverImage.large)||\'\',score:m.averageScore?Math.round(m.averageScore)/10:null,year:m.seasonYear||null,eps:m.episodes||m.chapters||null,type:m.format||\'\',syn:m.description}}\nfunction nm(it){var im=(it.images&&it.images.jpg)||{};return{id:it.mal_id,title:it.title_english||it.title||\'?\',img:im.large_image_url||im.image_url||\'\',score:it.score,syn:(it.synopsis||\'\').trim(),type:it.type,eps:it.episodes||it.chapters}}\nfunction fin(a){return a.filter(function(m){return m&&m.idMal&&!m.isAdult}).map(alm)}\nfunction season(){var d=new Date();return{s:[\'WINTER\',\'SPRING\',\'SUMMER\',\'FALL\'][Math.floor(d.getMonth()/3)],y:d.getFullYear()}}\nfunction alList(kind,o,pg){\n if(kind==\'search\')return AL(\'query($q:String){Page(page:1,perPage:30){media(type:ANIME,search:$q){\'+MF+\'}}}\',{q:o.q}).then(function(d){return fin(d.Page.media)});\n var z=season();return AL(\'query($p:Int,$s:MediaSeason,$y:Int){Page(page:$p,perPage:30){media(type:ANIME,season:$s,seasonYear:$y,sort:POPULARITY_DESC){\'+MF+\'}}}\',{p:pg,s:z.s,y:z.y}).then(function(d){return fin(d.Page.media)})}\nfunction jkList(kind,o,pg){\n return get(JK+\'/seasons/now?limit=24&page=\'+pg).then(function(d){return (d.data||[]).map(nm)})\n}\nfunction L(kind,o){o=o||{};var pg=o.page||1;return alList(kind,o,pg).catch(function(){return jkList(kind,o,pg)})\n}\nfunction M(id,kind){return AL(\'query($i:Int,$t:MediaType){Media(idMal:$i,type:$t){\'+MF+\'}}\',{i:id,t:kind==\'manga\'?\'MANGA\':\'ANIME\'}).then(function(d){return alm(d.Media)})\n.catch(function(){return get(JK+\'/anime/\'+id).then(function(d){return nm(d.data)})})}\nfunction skI(n){var h=\'\';for(var i=0;i<n;i++)h+=\'<div class="tile"><div class="sk" style="aspect-ratio:2/3;border-radius:14px"></div></div>\';return h}\nfunction skT(n){return\'<div class="grid">\'+skI(n)+\'</div>\'}\nfunction skC(n){var h=\'\';for(var i=0;i<n;i++)h+=\'<div class="st"><div class="sk" style="width:72px;height:72px;border-radius:50%"></div></div>\';return h}\nfunction tileItems(l,k){return l.map(function(m){return\'<a class="tile" href="/serie/\'+m.id+\'"><img src="\'+esc(m.img)+\'" loading="lazy" alt=""><b>\'+esc(m.title)+\'</b></a>\'}).join(\'\')}\nfunction posters(l){return l.map(function(m){return\'<a class="st" style="width:96px" href="/serie/\'+m.id+\'"><img src="\'+esc(m.img)+\'" alt="" loading="lazy" style="border-radius:14px;aspect-ratio:2/3"><span>\'+esc(m.title)+\'</span></a>\'}).join(\'\')}\n'

try:
    init_db()
except Exception:
    log.exception("Init db error")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)


import os, json, base64, uuid, re, time, logging, hashlib, secrets, string, random
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
try:
    from pytrends.request import TrendReq as _TrendReq
    _PYTRENDS_OK = True
except ImportError:
    _PYTRENDS_OK = False
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai import types
from PIL import Image
import io
from dotenv import load_dotenv
import requests
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import redis as redis_lib

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('atelier')

app = Flask(__name__)

# ── Secret key ─────────────────────────────────────────────────────────────────
# IMPORTANT: set SECRET_KEY in env vars on Railway.
# Without it, each worker process generates a different random key, causing
# sessions signed by one worker to be rejected by another → random logouts.
_secret_key = os.getenv('SECRET_KEY')
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    logging.warning(
        '[security] SECRET_KEY env var is not set — using an ephemeral random key. '
        'Sessions will NOT persist across restarts or workers. '
        'Set SECRET_KEY in your Railway environment variables.'
    )
app.secret_key = _secret_key
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)

CORS(app, supports_credentials=True)

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
_DB_URL = os.getenv('DATABASE_URL', '')

def get_db():
    """Return a new psycopg2 connection. Caller must close it."""
    if not _DB_URL:
        raise RuntimeError('DATABASE_URL is not set')
    conn = psycopg2.connect(_DB_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    return conn

@contextmanager
def db_conn():
    """Context manager that opens a connection, commits on success, rolls back
    and re-raises on error, and always closes the connection.

    Usage:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Create tables if they don't exist yet."""
    if not _DB_URL:
        log.warning('[db] DATABASE_URL not set — skipping DB init, falling back to JSON file')
        return
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                # ── Auth tables ──────────────────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        email        TEXT UNIQUE NOT NULL,
                        password_hash TEXT,
                        google_id    TEXT UNIQUE,
                        display_name TEXT NOT NULL DEFAULT '',
                        avatar_url   TEXT NOT NULL DEFAULT '',
                        verified     BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_login   TIMESTAMPTZ
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS otp_codes (
                        id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        email      TEXT NOT NULL,
                        code       TEXT NOT NULL,
                        purpose    TEXT NOT NULL DEFAULT 'verify',
                        expires_at TIMESTAMPTZ NOT NULL,
                        used       BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_otp_email ON otp_codes(email);
                """)
                # ── App tables ───────────────────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS categories (
                        name        TEXT PRIMARY KEY,
                        description TEXT NOT NULL DEFAULT '',
                        is_set      BOOLEAN NOT NULL DEFAULT FALSE,
                        templates   JSONB NOT NULL DEFAULT '[]'::jsonb,
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gallery (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
                        url         TEXT NOT NULL,
                        type        TEXT NOT NULL DEFAULT 'image',
                        category    TEXT NOT NULL DEFAULT '',
                        title       TEXT NOT NULL DEFAULT '',
                        tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                # Migrate: add user_id column if it was missing (existing deploys)
                cur.execute("""
                    ALTER TABLE gallery ADD COLUMN IF NOT EXISTS
                    user_id UUID REFERENCES users(id) ON DELETE CASCADE;
                """)
                # Migrate: add thumbnail_url for 3D model gallery items
                cur.execute("""
                    ALTER TABLE gallery ADD COLUMN IF NOT EXISTS
                    thumbnail_url TEXT NOT NULL DEFAULT '';
                """)
                # Migrate: add model_urls JSONB for storing all download links
                cur.execute("""
                    ALTER TABLE gallery ADD COLUMN IF NOT EXISTS
                    model_urls JSONB NOT NULL DEFAULT '{}'::jsonb;
                """)
                # ── Projects table (v2: folder / version grouping) ────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        name        TEXT NOT NULL DEFAULT 'Untitled Project',
                        description TEXT NOT NULL DEFAULT '',
                        category    TEXT NOT NULL DEFAULT '',
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);")
                # Migrate: add project_id to gallery
                cur.execute("""
                    ALTER TABLE gallery ADD COLUMN IF NOT EXISTS
                    project_id UUID REFERENCES projects(id) ON DELETE SET NULL;
                """)
                # Migrate: add version number to gallery
                cur.execute("""
                    ALTER TABLE gallery ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;
                """)
                # ── Usage / quota tracking ────────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS api_usage (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
                        endpoint    TEXT NOT NULL,
                        provider    TEXT NOT NULL DEFAULT '',
                        tokens_used INT NOT NULL DEFAULT 0,
                        cost_usd    NUMERIC(10,6) NOT NULL DEFAULT 0,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_user ON api_usage(user_id, created_at);")
                # ── BOM / costing table ───────────────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bom_reports (
                        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id       UUID REFERENCES users(id) ON DELETE CASCADE,
                        gallery_id    UUID REFERENCES gallery(id) ON DELETE CASCADE,
                        category      TEXT NOT NULL DEFAULT '',
                        metal         TEXT NOT NULL DEFAULT '',
                        metal_weight_g NUMERIC(8,3),
                        stone_details JSONB NOT NULL DEFAULT '[]'::jsonb,
                        labour_hrs    NUMERIC(6,2),
                        est_cost_inr  NUMERIC(12,2),
                        report_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                # Seed categories from DEFAULT_CATEGORIES if table is empty
                cur.execute('SELECT COUNT(*) AS n FROM categories')
                row = cur.fetchone()
                if row['n'] == 0:
                    log.info('[db] seeding categories table from defaults')
                    for name, meta in DEFAULT_CATEGORIES.items():
                        cur.execute(
                            """INSERT INTO categories (name, description, is_set, templates)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT (name) DO NOTHING""",
                            (
                                name,
                                meta.get('description', ''),
                                meta.get('is_set', False),
                                json.dumps(meta.get('templates', [])),
                            )
                        )
        log.info('[db] tables ready')
    except Exception as e:
        log.error(f'[db] init_db failed: {e}', exc_info=True)

# ── Redis ──────────────────────────────────────────────────────────────────────
_REDIS_URL = os.getenv('REDIS_URL', '')
_redis: redis_lib.Redis | None = None
_redis_lock = __import__('threading').Lock()

def get_redis() -> redis_lib.Redis | None:
    """Return the Redis client, or None if unavailable.
    Thread-safe: uses a lock to prevent double-initialisation under gevent workers.
    """
    global _redis
    if _redis is not None:
        return _redis
    if not _REDIS_URL:
        return None
    with _redis_lock:
        # Re-check inside the lock (double-checked locking)
        if _redis is not None:
            return _redis
        try:
            client = redis_lib.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=2)
            client.ping()
            _redis = client
            log.info('[redis] connected')
        except Exception as e:
            log.warning(f'[redis] unavailable, caching disabled: {e}')
            _redis = None
    return _redis

def cache_get(key: str):
    r = get_redis()
    if r is None:
        return None
    try:
        val = r.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None

def cache_set(key: str, value, ttl: int = 3600):
    r = get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl, json.dumps(value))
    except Exception:
        pass

def cache_del(key: str):
    r = get_redis()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception:
        pass

@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({'error': 'File too large. Maximum upload size is 32 MB.'}), 413

@app.before_request
def _log_req():
    from flask import g
    g.start_time = time.time()
    log.info(f"→ {request.method} {request.path}")

@app.after_request
def _log_res(response):
    from flask import g
    d = (time.time() - g.get('start_time', time.time())) * 1000
    lvl = logging.WARNING if response.status_code >= 400 else logging.INFO
    log.log(lvl, f"← {request.path} | {response.status_code} | {d:.0f}ms")
    return response

# ── Paths ──────────────────────────────────────────────────────────────────────
_IS_RAILWAY = os.getenv('RAILWAY_ENVIRONMENT') is not None
# On Railway, /data is a persistent volume that survives redeploys.
# Locally, use the current directory.
_DATA_ROOT = '/data' if _IS_RAILWAY else '.'

app.config['UPLOAD_FOLDER'] = os.path.join(_DATA_ROOT, 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(_DATA_ROOT, 'outputs')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
CATEGORIES_FILE = os.path.join(_DATA_ROOT, 'categories.json')  # fallback only

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# ── Default categories ─────────────────────────────────────────────────────────
DEFAULT_CATEGORIES = {
    "Necklace": {"description": "Neck jewellery worn around the neck", "templates": [
        {"name": "Choker", "placement": "sits tight around the neck base", "size_hint": "35-41cm, sits at throat level", "model_pose": "front-facing, chin slightly up"},
        {"name": "Princess", "placement": "rests on collarbone", "size_hint": "43-50cm, at collarbone", "model_pose": "front-facing, natural posture"},
        {"name": "Matinee", "placement": "falls between collarbone and bust", "size_hint": "50-60cm, mid-chest", "model_pose": "slight three-quarter turn"},
        {"name": "Opera", "placement": "long chain reaching sternum/below bust", "size_hint": "70-90cm, below bust", "model_pose": "slight lean, three-quarter view"},
    ]},
    "Earrings": {"description": "Ear jewellery", "templates": [
        {"name": "Stud", "placement": "sits flush on earlobe", "size_hint": "4-10mm diameter", "model_pose": "three-quarter face turn, hair swept back"},
        {"name": "Hoop", "placement": "circular ring through earlobe", "size_hint": "20-50mm diameter", "model_pose": "profile or three-quarter, hair back"},
        {"name": "Drop/Dangle", "placement": "hangs below earlobe", "size_hint": "3-7cm drop from lobe", "model_pose": "three-quarter turn, head slightly tilted"},
        {"name": "Chandelier", "placement": "multi-tier dramatic drop", "size_hint": "5-10cm elaborate drop", "model_pose": "face forward, chin up, hair pinned up"},
    ]},
    "Ring": {"description": "Finger jewellery", "templates": [
        {"name": "Solitaire", "placement": "single stone on band", "size_hint": "2-4mm band, stone 5-8mm", "model_pose": "hand extended forward, fingers spread"},
        {"name": "Cocktail/Statement", "placement": "large decorative ring", "size_hint": "large stone 10-20mm", "model_pose": "hand raised, fingers elegantly spread"},
        {"name": "Band", "placement": "simple flat band", "size_hint": "2-8mm width", "model_pose": "hand natural, slight angle"},
    ]},
    "Bracelet": {"description": "Wrist jewellery", "templates": [
        {"name": "Tennis Bracelet", "placement": "delicate in-line stones around wrist", "size_hint": "17-19cm, 3-5mm wide", "model_pose": "wrist extended, arm slightly bent"},
        {"name": "Bangle", "placement": "rigid circular bracelet", "size_hint": "60-65mm inner diameter", "model_pose": "arm raised, wrist turned outward"},
        {"name": "Cuff", "placement": "open-ended wide bracelet", "size_hint": "wide 2-5cm, open at back", "model_pose": "forearm forward, wrist turned"},
    ]},
    "Anklet": {"description": "Ankle jewellery", "templates": [
        {"name": "Delicate Chain", "placement": "thin chain around ankle", "size_hint": "22-25cm, fine chain", "model_pose": "leg extended, bare ankle visible"},
        {"name": "Charm Anklet", "placement": "chain with small charms", "size_hint": "22-26cm with dangling charms", "model_pose": "walking pose or seated showing anklet"},
    ]},
    "Brooch": {"description": "Pin/brooch for garments", "templates": [
        {"name": "Lapel Pin", "placement": "pinned to jacket lapel", "size_hint": "2-3cm, left chest lapel", "model_pose": "three-quarter turn, jacket visible"},
        {"name": "Statement Brooch", "placement": "large decorative chest piece", "size_hint": "5-10cm, upper chest area", "model_pose": "front facing, upper body shot"},
    ]},
    "Jewellery Set": {"description": "A complete matching jewellery set", "is_set": True, "templates": [
        {"name": "Necklace + Drop Earrings Set", "placement": "necklace rests on collarbone; matching drop earrings hang from both earlobes", "size_hint": "necklace 43-70cm; earrings 3-7cm drop", "model_pose": "front-facing, chin slightly up, hair pinned up", "pieces": ["necklace", "earrings"], "set_instruction": "BOTH necklace AND earrings must appear on the model simultaneously."},
    ]},
}

_CATEGORIES_CACHE_KEY = 'glymr:categories'

def load_categories() -> dict:
    """Load categories from Redis cache → Postgres → JSON file fallback."""
    # 1. Redis cache
    cached = cache_get(_CATEGORIES_CACHE_KEY)
    if cached is not None:
        return cached

    # 2. Postgres
    if _DB_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT name, description, is_set, templates FROM categories ORDER BY name')
                    rows = cur.fetchall()
            if rows:
                cats = {
                    r['name']: {
                        'description': r['description'],
                        'is_set': r['is_set'],
                        'templates': r['templates'],
                    }
                    for r in rows
                }
                cache_set(_CATEGORIES_CACHE_KEY, cats, ttl=900)
                return cats
        except Exception as e:
            log.error(f'[db] load_categories failed: {e}', exc_info=True)

    # 3. JSON file fallback
    if os.path.exists(CATEGORIES_FILE):
        with open(CATEGORIES_FILE, 'r') as f:
            cats = json.load(f)
        cache_set(_CATEGORIES_CACHE_KEY, cats, ttl=300)
        return cats

    save_categories(DEFAULT_CATEGORIES)
    return DEFAULT_CATEGORIES


def save_categories(cats: dict):
    """Persist categories to Postgres (primary) and JSON file (fallback). Bust cache."""
    # 1. Postgres
    if _DB_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    # Upsert every category
                    for name, meta in cats.items():
                        cur.execute(
                            """INSERT INTO categories (name, description, is_set, templates, updated_at)
                               VALUES (%s, %s, %s, %s, NOW())
                               ON CONFLICT (name) DO UPDATE SET
                                   description = EXCLUDED.description,
                                   is_set      = EXCLUDED.is_set,
                                   templates   = EXCLUDED.templates,
                                   updated_at  = NOW()""",
                            (
                                name,
                                meta.get('description', ''),
                                meta.get('is_set', False),
                                json.dumps(meta.get('templates', [])),
                            )
                        )
                    # Remove categories deleted from the dict
                    cur.execute(
                        'DELETE FROM categories WHERE name <> ALL(%s)',
                        (list(cats.keys()),)
                    )
        except Exception as e:
            log.error(f'[db] save_categories failed: {e}', exc_info=True)

    # 2. JSON file fallback
    try:
        with open(CATEGORIES_FILE, 'w') as f:
            json.dump(cats, f, indent=2)
    except Exception as e:
        log.warning(f'[file] save_categories json fallback failed: {e}')

    # 3. Bust Redis cache
    cache_del(_CATEGORIES_CACHE_KEY)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_gemini_client():
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return None, "GEMINI_API_KEY not set in .env file"
    # Set an explicit 180-second timeout so slow calls (e.g. market-research
    # with Google Search grounding) raise a clean TimeoutError rather than
    # letting Gunicorn's worker-kill (SystemExit) tear through mid-stream.
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=180_000),  # milliseconds
    )
    return client, None

def get_meshy_key():
    key = os.getenv('MESHY_API_KEY')
    if not key:
        return None, "MESHY_API_KEY not set in .env file"
    return key, None

# ── Auth helpers ───────────────────────────────────────────────────────────────

def _otp_code(length=6):
    return ''.join(random.choices(string.digits, k=length))

def _send_otp_gmail(to_email: str, code: str, purpose: str = 'verify', access_token: str | None = None) -> bool:
    """Send OTP via Gmail API using a fresh OAuth2 access token."""
    gmail_token = access_token or os.getenv('GMAIL_OAUTH_TOKEN')
    gmail_sender = os.getenv('GMAIL_SENDER_EMAIL')
    if not gmail_token or not gmail_sender:
        log.error('[otp] GMAIL_OAUTH_TOKEN or GMAIL_SENDER_EMAIL not set')
        return False
    subject_map = {'verify': 'Verify your glymr account', 'login': 'Your glymr sign-in code'}
    subject = subject_map.get(purpose, 'Your glymr code')
    body = (
        f"Subject: {subject}\r\n"
        f"To: {to_email}\r\n"
        f"From: glymr Studio <{gmail_sender}>\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n\r\n"
        f"<div style='font-family:sans-serif;max-width:480px;margin:40px auto;padding:32px;border:1px solid #e8e0d2;border-radius:10px;background:#faf7f2'>"
        f"<div style='font-size:28px;margin-bottom:4px'>◈ glymr</div>"
        f"<h2 style='font-size:20px;margin:16px 0 8px'>Your verification code</h2>"
        f"<p style='color:#5a5047;margin-bottom:24px'>Enter this code to {'verify your account' if purpose=='verify' else 'sign in'}. It expires in 10 minutes.</p>"
        f"<div style='font-size:40px;font-weight:700;letter-spacing:10px;text-align:center;padding:20px;background:#f2ede4;border-radius:8px;margin-bottom:24px'>{code}</div>"
        f"<p style='color:#5a5047;font-size:13px'>If you didn't request this, you can safely ignore this email.</p>"
        f"</div>"
    )
    raw = base64.urlsafe_b64encode(body.encode()).decode()
    try:
        r = requests.post(
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
            headers={'Authorization': f'Bearer {gmail_token}', 'Content-Type': 'application/json'},
            json={'raw': raw},
            timeout=10,
        )
        if r.status_code in (200, 202):
            log.info(f'[otp] sent to {to_email}')
            return True
        log.error(f'[otp] Gmail API error {r.status_code}: {r.text[:200]}')
        return False
    except Exception as e:
        log.error(f'[otp] send failed: {e}')
        return False


def _get_gmail_access_token() -> str | None:
    """Exchange refresh token for a fresh access token (called lazily per request)."""
    refresh_token = os.getenv('GMAIL_REFRESH_TOKEN')
    client_id     = os.getenv('GMAIL_CLIENT_ID') or os.getenv('GOOGLE_CLIENT_ID')
    client_secret = os.getenv('GMAIL_CLIENT_SECRET') or os.getenv('GOOGLE_CLIENT_SECRET')
    if not all([refresh_token, client_id, client_secret]):
        return os.getenv('GMAIL_OAUTH_TOKEN')  # fallback: static token in env
    try:
        r = requests.post('https://oauth2.googleapis.com/token', data={
            'grant_type':    'refresh_token',
            'refresh_token': refresh_token,
            'client_id':     client_id,
            'client_secret': client_secret,
        }, timeout=8)
        body = r.json()
        if 'access_token' not in body:
            log.error(f'[gmail-token] refresh returned no access_token: {body.get("error")}: {body.get("error_description")}')
            return None
        return body['access_token']
    except Exception as e:
        log.error(f'[gmail-token] refresh failed: {e}')
        return None


def send_otp(to_email: str, purpose: str = 'verify') -> bool:
    """Generate, store, and email an OTP. Returns True on success."""
    if not _DB_URL:
        return False
    code = _otp_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                # Invalidate previous unused codes for this email+purpose
                cur.execute(
                    "UPDATE otp_codes SET used=TRUE WHERE email=%s AND purpose=%s AND used=FALSE",
                    (to_email, purpose)
                )
                cur.execute(
                    "INSERT INTO otp_codes (email, code, purpose, expires_at) VALUES (%s,%s,%s,%s)",
                    (to_email, code, purpose, expires)
                )
    except Exception as e:
        log.error(f'[otp] db error: {e}')
        return False
    token = _get_gmail_access_token()
    if not token:
        log.error('[otp] Could not obtain a Gmail access token — check GMAIL_REFRESH_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET (or GMAIL_OAUTH_TOKEN as fallback)')
        return False
    return _send_otp_gmail(to_email, code, purpose, access_token=token)


def verify_otp(email: str, code: str, purpose: str = 'verify') -> bool:
    """Check OTP. Marks it used if valid. Returns True on success."""
    if not _DB_URL:
        return False
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id FROM otp_codes
                       WHERE email=%s AND code=%s AND purpose=%s AND used=FALSE AND expires_at > NOW()
                       ORDER BY created_at DESC LIMIT 1""",
                    (email, code.strip(), purpose)
                )
                row = cur.fetchone()
                if not row:
                    return False
                cur.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (row['id'],))
        return True
    except Exception as e:
        log.error(f'[otp] verify error: {e}')
        return False


def get_current_user():
    """Return current user dict from session, or None."""
    uid = session.get('user_id')
    if not uid:
        return None
    if not _DB_URL:
        return None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT id, email, display_name, avatar_url, verified FROM users WHERE id=%s',
                    (uid,)
                )
                row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required', 'redirect': '/auth'}), 401
            return redirect(url_for('auth_page', next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Per-user rate limiting helpers ────────────────────────────────────────────

_RATE_LIMITS = {
    '/api/generate-cad':        (10, 3600),   # 10 per hour
    '/api/generate-cad-multi':  (10, 3600),
    '/api/conceptualise':       (20, 3600),
    '/api/generate-image':      (20, 3600),
    '/api/market-research':     (15, 3600),
    '/api/trends':              (30, 3600),   # 30 trend lookups per hour
    '/api/validate-cad':        (30, 3600),
    '/api/enhance-sketch':      (20, 3600),
}

def _rate_limit_check(endpoint: str, user_id: str) -> tuple[bool, int]:
    """Returns (allowed, seconds_until_reset). Uses Redis sliding window."""
    if endpoint not in _RATE_LIMITS:
        return True, 0
    limit, window = _RATE_LIMITS[endpoint]
    r = get_redis()
    if r is None:
        return True, 0  # no Redis → skip limiting
    key = f'glymr:rl:{user_id}:{endpoint}'
    try:
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = pipe.execute()
        if count > limit:
            ttl = r.ttl(key)
            return False, max(ttl, 1)
        return True, 0
    except Exception:
        return True, 0

def track_usage(endpoint: str, provider: str = '', tokens: int = 0, cost_usd: float = 0.0):
    """Persist API usage record for the current user (best-effort, non-blocking)."""
    user_id = session.get('user_id')
    if not _DB_URL or not user_id:
        return
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO api_usage (user_id, endpoint, provider, tokens_used, cost_usd)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (user_id, endpoint, provider, tokens, cost_usd)
                )
    except Exception as e:
        log.warning(f'[usage] track failed: {e}')

def rate_limited(f):
    """Decorator that enforces per-user rate limits defined in _RATE_LIMITS."""
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get('user_id', 'anon')
        allowed, reset_in = _rate_limit_check(request.path, uid)
        if not allowed:
            return jsonify({
                'error': f'Rate limit exceeded. Try again in {reset_in}s.',
                'retry_after': reset_in,
            }), 429
        return f(*args, **kwargs)
    return decorated


# ── Auth Routes ────────────────────────────────────────────────────────────────

@app.route('/auth')
def auth_page():
    if session.get('user_id'):
        return redirect(url_for('page_sketch'))
    google_client_id = os.getenv('GOOGLE_CLIENT_ID', '')
    next_url = request.args.get('next', '/studio/sketch')
    return render_template('auth.html', active_nav='', google_client_id=google_client_id, next_url=next_url)


@app.route('/auth/logout')
def auth_logout():
    session.clear()
    return redirect(url_for('landing'))


@app.route('/api/auth/send-otp', methods=['POST'])
def api_send_otp():
    data  = request.json or {}
    email = data.get('email', '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    # Determine purpose: if user exists & verified → login, else verify
    purpose = 'login'
    if _DB_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT id, verified FROM users WHERE email=%s', (email,))
                    row = cur.fetchone()
            if not row or not row['verified']:
                purpose = 'verify'
        except Exception:
            pass
    ok = send_otp(email, purpose)
    if not ok:
        return jsonify({'error': 'Failed to send OTP. Check GMAIL_OAUTH_TOKEN and GMAIL_SENDER_EMAIL env vars.'}), 500
    return jsonify({'success': True, 'purpose': purpose})


@app.route('/api/auth/verify-otp', methods=['POST'])
def api_verify_otp():
    data     = request.json or {}
    email    = data.get('email', '').strip().lower()
    code     = data.get('code', '').strip()
    password = data.get('password', '').strip()  # only for new accounts
    name     = data.get('name', '').strip()
    purpose  = data.get('purpose', 'verify')

    if not email or not code:
        return jsonify({'error': 'Email and code required'}), 400

    if not verify_otp(email, code, purpose):
        return jsonify({'error': 'Invalid or expired code'}), 400

    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id, email, display_name, avatar_url, verified FROM users WHERE email=%s', (email,))
                user = cur.fetchone()

                if user:
                    # Existing user: mark verified, update last_login
                    cur.execute(
                        'UPDATE users SET verified=TRUE, last_login=NOW() WHERE id=%s',
                        (user['id'],)
                    )
                    user_id = str(user['id'])
                    display = user['display_name'] or email.split('@')[0]
                    avatar  = user['avatar_url'] or ''
                else:
                    # New user: create account
                    pw_hash = generate_password_hash(password) if password else None
                    display = name or email.split('@')[0]
                    cur.execute(
                        """INSERT INTO users (email, password_hash, display_name, verified, last_login)
                           VALUES (%s,%s,%s,TRUE,NOW()) RETURNING id""",
                        (email, pw_hash, display)
                    )
                    user_id = str(cur.fetchone()['id'])
                    avatar  = ''

        session['user_id']    = user_id
        session['user_email'] = email
        session['user_name']  = display
        session['user_avatar']= avatar
        session.permanent     = True

        return jsonify({'success': True, 'user': {'id': user_id, 'email': email, 'name': display, 'avatar': avatar}})
    except Exception as e:
        log.error(f'[auth] verify-otp error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/google', methods=['POST'])
def api_auth_google():
    """Exchange Google OAuth code for user info and create/login user."""
    data         = request.json or {}
    access_token = data.get('access_token', '')
    if not access_token:
        return jsonify({'error': 'access_token required'}), 400
    try:
        # Fetch user info from Google
        r = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=8,
        )
        if r.status_code != 200:
            return jsonify({'error': 'Invalid Google token'}), 401
        g = r.json()
        google_id = g.get('id', '')
        email     = g.get('email', '').lower()
        name      = g.get('name', email.split('@')[0])
        avatar    = g.get('picture', '')
        if not email:
            return jsonify({'error': 'Could not get email from Google'}), 400

        if not _DB_URL:
            return jsonify({'error': 'DATABASE_URL not set'}), 500

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM users WHERE email=%s OR google_id=%s', (email, google_id))
                row = cur.fetchone()
                if row:
                    user_id = str(row['id'])
                    cur.execute(
                        'UPDATE users SET google_id=%s, display_name=%s, avatar_url=%s, verified=TRUE, last_login=NOW() WHERE id=%s',
                        (google_id, name, avatar, user_id)
                    )
                else:
                    cur.execute(
                        """INSERT INTO users (email, google_id, display_name, avatar_url, verified, last_login)
                           VALUES (%s,%s,%s,%s,TRUE,NOW()) RETURNING id""",
                        (email, google_id, name, avatar)
                    )
                    user_id = str(cur.fetchone()['id'])

        session['user_id']    = user_id
        session['user_email'] = email
        session['user_name']  = name
        session['user_avatar']= avatar
        session.permanent     = True

        return jsonify({'success': True, 'user': {'id': user_id, 'email': email, 'name': name, 'avatar': avatar}})
    except Exception as e:
        log.error(f'[auth] google error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/me')
def api_auth_me():
    user = get_current_user()
    if not user:
        return jsonify({'authenticated': False}), 401
    return jsonify({'authenticated': True, 'user': {
        'id':     str(user['id']),
        'email':  user['email'],
        'name':   user['display_name'],
        'avatar': user['avatar_url'],
    }})


@app.route('/api/auth/signin', methods=['POST'])
def api_auth_signin():
    """Sign in with email + password (for users who registered via email/OTP)."""
    data     = request.json or {}
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT id, email, display_name, avatar_url, password_hash, verified FROM users WHERE email=%s',
                    (email,)
                )
                user = cur.fetchone()
    except Exception as e:
        log.error(f'[auth] signin db error: {e}', exc_info=True)
        return jsonify({'error': 'Server error'}), 500

    if not user:
        return jsonify({'error': 'No account found with that email'}), 401

    if not user['verified']:
        return jsonify({'error': 'Account not verified. Please sign up again to verify your email.'}), 401

    if not user['password_hash']:
        return jsonify({'error': 'This account uses Google sign-in. Please use "Continue with Google" instead.'}), 401

    if not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Incorrect password'}), 401

    # Update last_login
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE users SET last_login=NOW() WHERE id=%s', (user['id'],))
    except Exception:
        pass

    user_id = str(user['id'])
    display  = user['display_name'] or email.split('@')[0]
    avatar   = user['avatar_url'] or ''

    session['user_id']     = user_id
    session['user_email']  = email
    session['user_name']   = display
    session['user_avatar'] = avatar
    session.permanent      = True

    log.info(f'[auth] password sign-in: {email}')
    return jsonify({'success': True, 'user': {'id': user_id, 'email': email, 'name': display, 'avatar': avatar}})


# ── Health check ───────────────────────────────────────────────────────────────

@app.route('/healthz')
def healthz():
    """Lightweight liveness check for Railway / load balancers."""
    status = {'status': 'ok', 'db': False, 'redis': False}
    if _DB_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT 1')
            status['db'] = True
        except Exception:
            pass
    if get_redis() is not None:
        status['redis'] = True
    return jsonify(status), 200


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route('/')
def landing():
    user = get_current_user()
    return render_template('landing.html', active_nav='', current_user=user)

@app.route('/studio/sketch')
@login_required
def page_sketch():
    cats = load_categories()
    return render_template('sketch.html', categories=list(cats.keys()), active_nav='sketch', current_user=get_current_user())

@app.route('/studio/model')
@login_required
def page_model():
    cats = load_categories()
    return render_template('model.html', categories=list(cats.keys()), active_nav='model', current_user=get_current_user())

@app.route('/studio/cad')
@login_required
def page_cad():
    cats = load_categories()
    return render_template('cad.html', categories=list(cats.keys()), active_nav='cad', current_user=get_current_user())

@app.route('/studio/market')
@login_required
def page_market():
    cats = load_categories()
    return render_template('market.html', categories=list(cats.keys()), active_nav='market', current_user=get_current_user())

@app.route('/studio/gallery')
@login_required
def page_gallery():
    return render_template('gallery.html', active_nav='gallery', current_user=get_current_user())

@app.route('/studio/projects')
@login_required
def page_projects():
    return render_template('projects.html', active_nav='projects', current_user=get_current_user())

# ── API: Categories ────────────────────────────────────────────────────────────

@app.route('/api/categories', methods=['GET'])
@login_required
def get_categories():
    return jsonify(load_categories())

@app.route('/api/categories/add', methods=['POST'])
@login_required
def add_category():
    data = request.json
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()
    if not name:
        return jsonify({'error': 'Category name required'}), 400
    cats = load_categories()
    if name in cats:
        return jsonify({'error': 'Category already exists'}), 400
    cats[name] = {"description": description, "templates": []}
    save_categories(cats)
    return jsonify({'success': True, 'categories': cats})

@app.route('/api/suggest-templates', methods=['POST'])
@login_required
def suggest_templates():
    data = request.json
    category = data.get('category', '')
    description = data.get('description', '')
    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    cache_key = f'glymr:templates:{hashlib.sha1((category + description).encode()).hexdigest()}'
    cached = cache_get(cache_key)
    if cached:
        log.info(f'[suggest-templates] cache HIT | category={category}')
        return jsonify(cached)

    prompt = f"""You are a professional jewellery sizing and styling expert.
For the jewellery category: "{category}" ({description})
Generate 4-6 sizing templates for AI image generation.
Return ONLY a JSON array:
[{{"name":"Template name","placement":"exact placement on body","size_hint":"measurements","model_pose":"ideal pose/angle"}}]"""

    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = re.sub(r'^```(?:json)?\s*', '', response.text.strip())
        text = re.sub(r'\s*```$', '', text)
        templates = json.loads(text)
        cats = load_categories()
        if category in cats:
            existing = {t['name'] for t in cats[category].get('templates', [])}
            cats[category]['templates'].extend([t for t in templates if t['name'] not in existing])
            save_categories(cats)
        result = {'templates': templates, 'success': True}
        cache_set(cache_key, result, ttl=86400)  # cache 24 h — templates rarely change
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/add-template', methods=['POST'])
@login_required
def add_template():
    data = request.json
    category = data.get('category')
    template = data.get('template')
    cats = load_categories()
    if category not in cats:
        return jsonify({'error': 'Category not found'}), 404
    cats[category]['templates'].append(template)
    save_categories(cats)
    return jsonify({'success': True})

# ── API: Sketch Conceptualiser ─────────────────────────────────────────────────

@app.route('/api/conceptualise', methods=['POST'])
@login_required
@rate_limited
def api_conceptualise():
    """
    Takes a sketch image (optional, as file upload OR base64) + text description
    + variations list.  Returns multiple concept objects with image_url, title,
    description, tags.  Each generated image is auto-saved to the user's gallery.
    """
    prompt_text  = request.form.get('prompt', '').strip()
    category     = request.form.get('category', 'Jewellery')
    metal        = request.form.get('metal', '22K Yellow Gold')
    variations   = json.loads(request.form.get('variations', '["Classic"]'))

    # ── Resolve sketch image ──────────────────────────────────────────────────
    # Priority: multipart file field 'sketch_image' → base64 form field 'sketch_data'
    sketch_img = None   # PIL Image to pass to Gemini
    has_sketch = False

    sketch_file = request.files.get('sketch_image')
    if sketch_file and sketch_file.filename:
        try:
            sketch_img = Image.open(io.BytesIO(sketch_file.read())).convert('RGB')
            has_sketch = True
            log.info('[conceptualise] using uploaded sketch file')
        except Exception as e:
            log.warning(f'[conceptualise] could not read sketch_image file: {e}')

    if not has_sketch:
        sketch_data = request.form.get('sketch_data', '')
        if sketch_data:
            try:
                b64 = sketch_data.split(',', 1)[1] if ',' in sketch_data else sketch_data
                sketch_img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGB')
                has_sketch = True
                log.info('[conceptualise] using base64 sketch_data')
            except Exception as e:
                log.warning(f'[conceptualise] could not decode sketch_data: {e}')

    if not prompt_text and not has_sketch:
        return jsonify({'error': 'Provide a description or upload a sketch.'}), 400

    # Cap variations to prevent runaway API usage (#13)
    variations = variations[:4]

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    user_id = session.get('user_id')
    concepts = []
    errors   = []

    def _generate_one_variation(variation: str) -> dict:
        """Generate a single concept variation. Retries up to 3 times on 503/504."""
        gen_prompt = f"""You are a senior jewellery design artist.
{"The user has provided a rough sketch of their jewellery concept." if has_sketch else ""}
Create a photorealistic, highly detailed jewellery product photograph of this concept:

Category: {category}
Metal: {metal}
Style Variation: {variation}
{"Description: " + prompt_text if prompt_text else ""}

Requirements:
- Professional jewellery product photography on a clean white/cream background
- Extremely detailed and realistic rendering of the {metal} metal
- Show intricate design elements, stone settings, textures clearly
- Soft studio lighting, no harsh shadows
- Magazine-quality jewellery catalogue style
- The piece should look like a real, wearable, high-end Indian jewellery piece

Generate an image that would be appropriate for a luxury jewellery brand's catalogue.
{"Interpret and improve the rough sketch, maintaining its core design intent." if has_sketch else ""}"""

        contents = [gen_prompt]
        if sketch_img is not None:
            contents.append(sketch_img)

        last_exc = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash-image",
                    contents=contents,
                    config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
                )
                break  # success
            except Exception as e:
                last_exc = e
                err_str = str(e)
                # Retry on transient server errors (503/504); fail fast on others
                if attempt < 2 and ('503' in err_str or '504' in err_str or
                                     'UNAVAILABLE' in err_str or 'DEADLINE_EXCEEDED' in err_str):
                    wait = 2 ** attempt  # 1s, 2s
                    log.warning(f'[conceptualise] transient error for {variation} (attempt {attempt+1}), retrying in {wait}s: {e}')
                    time.sleep(wait)
                    continue
                raise
        else:
            raise last_exc

        image_url = None
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                out_bytes = part.inline_data.data
                out_fn    = f"concept_{uuid.uuid4()}.png"

                # Try to persist to R2 for durable storage
                r2_url = _upload_to_r2(out_bytes, f'concepts/{out_fn}')
                if r2_url:
                    image_url = r2_url
                    log.info(f'[conceptualise] uploaded to R2: {r2_url}')
                else:
                    # Fall back to local filesystem
                    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                    Image.open(io.BytesIO(out_bytes)).save(out_path)
                    image_url = f'/static/outputs/{out_fn}'

                log.info(f'[conceptualise] concept image: {image_url} | variation={variation}')
                break

        # Generate metadata with a fast text call
        meta_prompt = f"""For a {variation.lower()} style {metal} {category} jewellery piece{(' described as: ' + prompt_text) if prompt_text else ''}, generate a short JSON object:
{{"title":"3-5 word evocative product name","description":"2 sentence description mentioning materials, style, and occasion","tags":["tag1","tag2","tag3","tag4"]}}
Return ONLY the JSON, no markdown."""

        meta_res = client.models.generate_content(model="gemini-2.5-flash", contents=meta_prompt)
        try:
            meta_text = re.sub(r'^```(?:json)?\s*', '', meta_res.text.strip())
            meta_text = re.sub(r'\s*```$', '', meta_text)
            meta = json.loads(meta_text)
        except Exception:
            meta = {"title": f"{variation} {category}", "description": f"A beautiful {variation.lower()} style {metal} {category}.", "tags": [metal, category, variation]}

        concept = {
            'variation':   variation,
            'image_url':   image_url,
            'title':       meta.get('title', f'{variation} {category}'),
            'description': meta.get('description', ''),
            'tags':        meta.get('tags', []),
        }

        # ── Auto-save to gallery ──────────────────────────────────────────────
        if image_url and user_id and _DB_URL:
            try:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO gallery (user_id, url, type, category, title, tags, thumbnail_url)
                               VALUES (%s, %s, 'concept', %s, %s, %s, %s)""",
                            (user_id, image_url, category,
                             meta.get('title', ''), json.dumps(meta.get('tags', [])),
                             image_url)   # concept image doubles as its own thumbnail
                        )
                log.info(f'[conceptualise] auto-saved to gallery: {image_url}')
            except Exception as db_err:
                log.warning(f'[conceptualise] gallery auto-save failed: {db_err}')

        return concept

    # ── Run all variations in parallel ────────────────────────────────────────
    with ThreadPoolExecutor(max_workers=len(variations)) as pool:
        future_to_var = {pool.submit(_generate_one_variation, v): v for v in variations}
        try:
            for future in as_completed(future_to_var, timeout=260):
                var = future_to_var[future]
                try:
                    concepts.append(future.result())
                except Exception as e:
                    log.error(f'[conceptualise] FAIL for variation={var}: {e}', exc_info=True)
                    errors.append(f'{var}: {str(e)}')
        except FuturesTimeoutError:
            log.error('[conceptualise] overall parallel timeout after 260s')
            errors.append('Overall timeout — some variations did not complete')

    if not concepts:
        return jsonify({'error': 'All variations failed. ' + '; '.join(errors)}), 500

    return jsonify({'concepts': concepts, 'errors': errors if errors else None})

# ── API: Model Image Generation (existing logic, cleaned up) ───────────────────

@app.route('/api/generate-image', methods=['POST'])
@login_required
@rate_limited
def generate_image():
    files = request.files.getlist('jewellery_image')
    if not files or not files[0].filename:
        return jsonify({'error': 'No image uploaded'}), 400

    category         = request.form.get('category', '')
    template_json    = request.form.get('template', '{}')
    custom_prompt    = request.form.get('custom_prompt', '')
    negative_prompt  = request.form.get('negative_prompt', '')
    model_preference = request.form.get('model_preference', 'diverse female model')
    duplication_guard = request.form.get('duplication_guard', 'false').lower() == 'true'

    try:
        template = json.loads(template_json)
    except Exception:
        template = {}

    saved_paths = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            fn = secure_filename(f"{uuid.uuid4()}_{file.filename}")
            fp = os.path.join(app.config['UPLOAD_FOLDER'], fn)
            file.save(fp)
            saved_paths.append(fp)

    if not saved_paths:
        return jsonify({'error': 'Invalid file type'}), 400

    placement     = template.get('placement', 'naturally on the model')
    size_hint     = template.get('size_hint', '')
    pose          = template.get('model_pose', 'natural elegant pose')
    template_name = template.get('name', category)
    set_instruction = template.get('set_instruction', '')
    pieces        = template.get('pieces', [])

    cats = load_categories()
    is_set = cats.get(category, {}).get('is_set', False) or 'set' in category.lower()

    if is_set:
        pieces_desc = ' and '.join(pieces) if pieces else 'all pieces in the set'
        prompt = f"""Professional high-end fashion photography. A beautiful model wearing this complete jewellery set.
⚠ THIS IS A JEWELLERY SET — ALL PIECES MUST BE WORN SIMULTANEOUSLY ⚠
{set_instruction if set_instruction else f'Show all pieces ({pieces_desc}) worn together.'}
Pieces: {pieces_desc} | Placement: {placement} | Size: {size_hint} | Pose: {pose}
Model: {model_preference} | Studio lighting, high-end catalogue quality.
{('Additional: ' + custom_prompt) if custom_prompt else ''}
{('Avoid: ' + negative_prompt) if negative_prompt else ''}"""
    else:
        dup_prefix = ""
        if duplication_guard:
            dup_prefix = f"⚠ CONSTRAINT: ONE (1) {category} on the model, worn exactly once. Not two, not three. ONE.\n\n"
        prompt = f"""{dup_prefix}Professional fashion photography. A beautiful model wearing this exact {category}.
Placement: {placement} | Size: {size_hint} | Pose: {pose} | Template: {template_name} | Model: {model_preference}
Studio lighting, high-end fashion magazine quality. Jewellery prominently visible, correctly sized.
RULES: 1) Reproduce jewellery identically from reference. 2) ONE {category} total. 3) No duplication.
{('Additional: ' + custom_prompt) if custom_prompt else ''}
{('Avoid: ' + negative_prompt) if negative_prompt else ''}"""

    return jsonify(_generate_with_gemini(saved_paths[0], prompt, category, extra_paths=saved_paths[1:]))

def _generate_with_gemini(image_path, prompt, category, extra_paths=None):
    client, err = get_gemini_client()
    if err:
        return {'error': err}
    try:
        img = Image.open(image_path)
        contents = [prompt, img]
        if extra_paths:
            contents[0] += f"\n\n{len(extra_paths)} additional angle(s) provided below."
            for ep in extra_paths:
                try: contents.append(Image.open(ep))
                except Exception: pass

        response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=contents,
            config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
        )

        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                out_bytes = part.inline_data.data
                out_fn    = f"output_{uuid.uuid4()}.png"

                # Try durable R2 storage first; fall back to local /tmp
                r2_url = _upload_to_r2(out_bytes, f'outputs/{out_fn}')
                if r2_url:
                    image_url = r2_url
                    log.info(f'[generate] uploaded to R2: {r2_url}')
                else:
                    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                    Image.open(io.BytesIO(out_bytes)).save(out_path)
                    image_url = f'/static/outputs/{out_fn}'

                return {'success': True, 'image_url': image_url, 'provider': 'Gemini', 'prompt_used': prompt}

        text_parts = [p.text for p in response.candidates[0].content.parts if p.text]
        return {'error': 'Gemini returned no image.', 'details': ' '.join(text_parts) if text_parts else 'No details.'}
    except Exception as e:
        err_str = str(e)
        hint = 'Check your GEMINI_API_KEY and image generation permissions.'
        if '429' in err_str or 'quota' in err_str.lower():
            hint = 'API rate limit hit. Wait a moment and try again.'
        elif '403' in err_str or 'permission' in err_str.lower():
            hint = 'API key lacks image generation access. Use a key from aistudio.google.com.'
        return {'error': f'Gemini failed: {err_str}', 'details': hint}

# ── API: Analyse result (AI feedback loop) ─────────────────────────────────────

@app.route('/api/analyze-result', methods=['POST'])
@login_required
def analyze_result():
    data = request.json
    original_src = data.get('original_src', '')
    generated_url = data.get('generated_url', '')
    category = data.get('category', '')
    template = data.get('template', {})
    current_prompt = data.get('current_prompt', '')
    current_negative = data.get('current_negative', '')

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    try:
        # Generated image may be stored in R2 (full https:// URL) or locally
        if generated_url.startswith('http://') or generated_url.startswith('https://'):
            try:
                r2_resp = requests.get(generated_url, timeout=15)
                r2_resp.raise_for_status()
                gen_image = Image.open(io.BytesIO(r2_resp.content))
            except Exception as fetch_err:
                return jsonify({'error': f'Could not fetch generated image from remote URL: {fetch_err}'}), 500
        else:
            gen_filename = generated_url.split('/static/outputs/')[-1].split('?')[0]
            gen_path = os.path.join(app.config['OUTPUT_FOLDER'], gen_filename)
            gen_image = Image.open(gen_path)

        if ',' in original_src:
            b64data = original_src.split(',', 1)[1]
        else:
            b64data = original_src
        orig_image = Image.open(io.BytesIO(base64.b64decode(b64data)))

        cats_data = load_categories()
        is_set = cats_data.get(category, {}).get('is_set', False) or 'set' in category.lower()

        analysis_prompt = f"""You are a jewellery photography AI prompt engineer.
Image 1: ORIGINAL jewellery | Image 2: AI-GENERATED model photo
Category: {category} | Template: {template.get('name','')}

Compare the two images and identify all discrepancies in design, placement, and accuracy.

Return ONLY this JSON:
{{"original_description":"spatial description","generated_description":"spatial description","issues":["issue1","issue2"],"refined_prompt":"improved generation prompt","refined_negative":"comma-separated negative terms"}}"""

        def pil_to_part(img):
            buf = io.BytesIO(); img.save(buf, format='PNG')
            return types.Part.from_bytes(data=buf.getvalue(), mime_type='image/png')

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[analysis_prompt, pil_to_part(orig_image), pil_to_part(gen_image)]
        )
        text = re.sub(r'^```(?:json)?\s*', '', response.text.strip())
        text = re.sub(r'\s*```$', '', text)
        result = json.loads(text)
        result['duplication_detected'] = False
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: 3D CAD (Meshy AI) ─────────────────────────────────────────────────────

def _upload_to_r2(img_bytes: bytes, filename: str) -> str | None:
    """
    Upload image bytes to Cloudflare R2 (or any S3-compatible store) and return
    a public URL.  Requires env vars:
        R2_ENDPOINT_URL  – e.g. https://<account>.r2.cloudflarestorage.com
        R2_ACCESS_KEY_ID
        R2_SECRET_ACCESS_KEY
        R2_BUCKET_NAME
        R2_PUBLIC_URL    – public base URL, e.g. https://assets.yourdomain.com
    Falls back to None if any variable is missing (caller uses base64 URI instead).
    """
    endpoint   = os.getenv('R2_ENDPOINT_URL', '').rstrip('/')
    access_key = os.getenv('R2_ACCESS_KEY_ID', '')
    secret_key = os.getenv('R2_SECRET_ACCESS_KEY', '')
    bucket     = os.getenv('R2_BUCKET_NAME', '')
    public_url = os.getenv('R2_PUBLIC_URL', '').rstrip('/')

    if not all([endpoint, access_key, secret_key, bucket, public_url]):
        return None

    try:
        try:
            import boto3
        except ImportError:
            log.warning('[cad] boto3 not installed — R2 upload unavailable. Run: pip install boto3')
            return None
        s3 = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name='auto',
        )
        # callers already pass the full subpath: 'concepts/…', 'outputs/…', 'cad-inputs/…'
        key = filename
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=img_bytes,
            ContentType='image/png',
        )
        return f'{public_url}/{key}'
    except Exception as e:
        log.warning(f'[cad] R2 upload failed, falling back to base64: {e}')
        return None


@app.route('/api/generate-cad', methods=['POST'])
@login_required
@rate_limited
def api_generate_cad():
    """Submit an image to Meshy AI image-to-3D and return a task_id for polling.

    Accepts the image either as:
      • a multipart file field  (name='image')  — preferred, sent by cad.html
      • a base64 data-URI string (name='image_data') — legacy / fallback

    Optional form field 'geometry_hint' — structured description from
    /api/extract-design-features — injected into the Meshy object_prompt
    to improve 3D reconstruction accuracy.
    """
    prompt        = request.form.get('prompt', '').strip()
    art_style     = request.form.get('art_style', 'realistic')
    target_use    = request.form.get('target_use', 'visualization')
    geometry_hint = request.form.get('geometry_hint', '').strip()

    meshy_key, err = get_meshy_key()
    if err:
        return jsonify({'error': err, 'details': 'Add MESHY_API_KEY to your .env file. Get one free at meshy.ai'}), 500

    # ── Resolve image bytes from either source ────────────────────────────────
    img_bytes = None

    uploaded_file = request.files.get('image')
    if uploaded_file and uploaded_file.filename:
        img_bytes = uploaded_file.read()
    else:
        image_data = request.form.get('image_data', '')
        if image_data:
            try:
                b64 = image_data.split(',', 1)[1] if ',' in image_data else image_data
                img_bytes = base64.b64decode(b64)
            except Exception as e:
                return jsonify({'error': f'Invalid base64 image data: {e}'}), 400

    if not img_bytes:
        return jsonify({'error': 'No image provided'}), 400

    try:
        # Normalise to PNG
        pil_img  = Image.open(io.BytesIO(img_bytes)).convert('RGBA')
        buf      = io.BytesIO()
        pil_img.save(buf, format='PNG')
        img_bytes = buf.getvalue()

        # Compress: if image is large, resize to max 1200px and re-encode as JPEG
        # This matters most for the base64 Meshy path where payload size is critical
        pil_img_rgb = pil_img.convert('RGB')
        MAX_DIM = 1200
        w, h = pil_img_rgb.size
        if w > MAX_DIM or h > MAX_DIM:
            ratio = min(MAX_DIM / w, MAX_DIM / h)
            pil_img_rgb = pil_img_rgb.resize(
                (int(w * ratio), int(h * ratio)), Image.LANCZOS
            )
        buf_jpg = io.BytesIO()
        pil_img_rgb.save(buf_jpg, format='JPEG', quality=88)
        img_bytes_compressed = buf_jpg.getvalue()

        tmp_fn = f"cad_input_{uuid.uuid4()}.png"

        # ── Attempt 1: upload to R2 / S3 for a public URL ────────────────────
        public_img_url = _upload_to_r2(img_bytes_compressed, f'cad-inputs/{tmp_fn}')

        # ── Attempt 2: use host URL (works when deployed publicly) ──────────
        if not public_img_url:
            tmp_path = os.path.join(app.config['UPLOAD_FOLDER'], tmp_fn)
            with open(tmp_path, 'wb') as f:
                f.write(img_bytes_compressed)
            host = request.host_url.rstrip('/')
            # On Railway the host is public; locally it resolves to localhost
            # which Meshy cannot reach — we fall through to base64 below.
            if 'localhost' not in host and '127.0.0.1' not in host:
                public_img_url = f"{host}/static/uploads/{tmp_fn}"

        # ── Build Meshy payload ───────────────────────────────────────────────
        style_map = {'realistic': 'realistic', 'jewelry': 'realistic', 'sculpture': 'sculpture', 'game': 'cartoon'}
        meshy_style = style_map.get(art_style, 'realistic')

        if public_img_url:
            log.info(f"[cad] using public image URL: {public_img_url}")
            payload = {
                "image_url": public_img_url,
                "enable_pbr": True,
                "should_remesh": True,
            }
        else:
            # ── Attempt 3: send compressed base64 — Meshy image-to-3D v1 supports it
            log.info("[cad] no public URL available, sending compressed base64 to Meshy")
            b64str = base64.b64encode(img_bytes_compressed).decode()
            payload = {
                "image_url": f"data:image/jpeg;base64,{b64str}",
                "enable_pbr": True,
                "should_remesh": True,
            }

        # Build the richest possible object_prompt:
        # geometry_hint (from feature extraction) takes precedence;
        # user prompt appended as extra context when both are present.
        object_prompt_parts = []
        if geometry_hint:
            object_prompt_parts.append(geometry_hint)
        if prompt:
            object_prompt_parts.append(prompt)
        if object_prompt_parts:
            payload["object_prompt"] = ' '.join(object_prompt_parts)[:500]

        headers = {
            "Authorization": f"Bearer {meshy_key}",
            "Content-Type": "application/json",
        }

        log.info(f"[cad] submitting to Meshy AI (style={meshy_style}) | prompt_len={len(payload.get('object_prompt',''))}")
        res = requests.post(
            "https://api.meshy.ai/openapi/v1/image-to-3d",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if res.status_code == 202:
            data = res.json()
            task_id = data.get('result')
            log.info(f"[cad] task submitted | task_id={task_id}")
            return jsonify({'task_id': task_id, 'success': True})
        else:
            log.error(f"[cad] Meshy error {res.status_code}: {res.text}")
            return jsonify({'error': f'Meshy API error: {res.status_code}', 'details': res.text[:300]}), 500

    except requests.exceptions.ConnectionError:
        return jsonify({
            'error': 'Cannot reach Meshy API.',
            'details': 'Check your network connection and MESHY_API_KEY. For local testing without a public URL, configure R2_ENDPOINT_URL/R2_BUCKET_NAME or deploy to Railway.'
        }), 500
    except Exception as e:
        log.error(f"[cad] exception: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/cad-status/<task_id>', methods=['GET'])
@login_required
def api_cad_status(task_id):
    """Poll Meshy for task status. Returns progress, status, and download URLs when done."""
    meshy_key, err = get_meshy_key()
    if err:
        return jsonify({'error': err}), 500

    try:
        res = requests.get(
            f"https://api.meshy.ai/openapi/v1/image-to-3d/{task_id}",
            headers={"Authorization": f"Bearer {meshy_key}"},
            timeout=15,
        )
        if res.status_code != 200:
            return jsonify({'error': f'Status check failed: {res.status_code}', 'status': 'FAILED'}), 500

        data = res.json()
        status   = data.get('status', 'UNKNOWN')
        progress = data.get('progress', 0)

        response = {
            'status':   status,
            'progress': progress,
            'task_id':  task_id,
        }

        if status == 'SUCCEEDED':
            model_urls = data.get('model_urls', {})
            response['model_urls'] = model_urls
            response['thumbnail_url'] = data.get('thumbnail_url', '')
            response['vertex_count'] = data.get('statistics', {}).get('vertex_count')
            response['face_count']   = data.get('statistics', {}).get('face_count')
            response['texture_resolution'] = data.get('statistics', {}).get('texture_resolution', '2048×2048')
            # Meshy viewer
            response['model_viewer_url'] = f"https://app.meshy.ai/models/{task_id}"
            log.info(f"[cad] SUCCEEDED | task_id={task_id} | urls={list(model_urls.keys())}")

        elif status == 'FAILED':
            response['error'] = data.get('task_error', {}).get('message', 'Unknown error')

        return jsonify(response)

    except Exception as e:
        log.error(f"[cad-status] exception: {e}", exc_info=True)
        return jsonify({'error': str(e), 'status': 'ERROR'}), 500

# ── API: CAD Validation & Manufacturability Check ─────────────────────────────

@app.route('/api/validate-cad', methods=['POST'])
@login_required
def api_validate_cad():
    """
    Analyse a jewellery image (or generated 3D thumbnail) for design integrity
    and manufacturing feasibility. Returns structured JSON with pass/fail checks,
    severity ratings, and actionable suggestions.
    """
    image_data  = request.form.get('image_data', '')
    category    = request.form.get('category', 'Jewellery').strip()
    target_use  = request.form.get('target_use', 'visualization').strip()
    metal       = request.form.get('metal', '').strip()

    client, err = get_gemini_client()
    if err:
        return jsonify({'success': False, 'error': err}), 500

    if not image_data:
        return jsonify({'success': False, 'error': 'No image provided for validation'}), 400

    # Cache key: SHA1 of image + category + target_use + metal
    if ',' in image_data:
        b64 = image_data.split(',', 1)[1]
    else:
        b64 = image_data
    try:
        img_bytes = base64.b64decode(b64)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Invalid image data: {e}'}), 400

    cache_key = 'glymr:validate:' + hashlib.sha1(
        img_bytes + category.encode() + target_use.encode() + metal.encode()
    ).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        log.info(f'[validate-cad] cache HIT | key={cache_key[:20]}…')
        return jsonify(cached)

    use_context = {
        'wax_casting':    'wax casting / lost-wax manufacturing for fine jewellery',
        '3d_print':       'FDM/SLA 3D printing followed by metal casting',
        'visualization':  'digital rendering and e-commerce visualisation only',
        'game':           'real-time game / AR / digital asset',
    }.get(target_use, target_use)

    metal_note = f'Metal/material: {metal}.' if metal else ''

    prompt = f"""You are a senior jewellery manufacturing engineer and CAD validation expert.
Analyse this {category} jewellery design image for structural integrity and manufacturing feasibility.
Target use: {use_context}. {metal_note}

Perform a thorough inspection across ALL of the following dimensions:

1. GEOMETRY — wall thickness, undercuts, fragile protrusions, unsupported spans
2. SYMMETRY — left/right balance, stone setting regularity, uniform prong spacing
3. STONE SETTINGS — prong count & height adequacy, bezel completeness, girdle exposure
4. STRUCTURAL INTEGRITY — weak joints, thin shanks, stress concentration points
5. MANUFACTURABILITY — tool access, parting line feasibility, casting shrinkage risk
6. PROPORTIONS — size relationships between elements, wearability ergonomics
7. SURFACE — tooling marks, texture consistency, polishability of concave areas

Return ONLY this exact JSON — no markdown, no extra keys:
{{
  "overall_score": <integer 0-100>,
  "overall_status": "<PASS|WARN|FAIL>",
  "summary": "<2-3 sentence plain-English verdict>",
  "checks": [
    {{
      "id": "<snake_case_id>",
      "label": "<Short label, max 5 words>",
      "status": "<pass|warn|fail>",
      "severity": "<low|medium|high>",
      "description": "<1-2 sentence finding>",
      "suggestion": "<1 sentence actionable fix, or null if pass>"
    }}
  ],
  "critical_issues": ["<issue 1>", "<issue 2>"],
  "recommendations": ["<recommendation 1>", "<recommendation 2>", "<recommendation 3>"]
}}

RULES:
- Include exactly one check per dimension above (7 checks total).
- overall_score: 90-100=excellent, 70-89=good with minor issues, 50-69=needs rework, <50=significant problems.
- overall_status: PASS if score>=75, WARN if 50-74, FAIL if <50.
- critical_issues: list only severity=high fail/warn items; empty array if none.
- Be specific — reference actual features visible in the image, not generic boilerplate."""

    try:
        img_part = types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg')
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[img_part, prompt],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        raw = response.text.strip().replace('```json', '').replace('```', '').strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return jsonify({'success': False, 'error': 'No JSON in response. Raw: ' + raw[:200]}), 500
        parsed = json.loads(match.group(0))
        result = {'success': True, **parsed}
        cache_set(cache_key, result, ttl=1800)  # 30 min cache
        log.info(f'[validate-cad] done | category={category} | score={parsed.get("overall_score")}')
        return jsonify(result)
    except Exception as e:
        log.error(f'[validate-cad] exception: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Sketch Enhancement (Gemini cleans 2D sketch before 3D) ───────────────

@app.route('/api/enhance-sketch', methods=['POST'])
@login_required
def api_enhance_sketch():
    """
    Gemini-powered 2D sketch enhancement step.

    Takes a rough hand-drawn or uploaded sketch image and produces a clean,
    production-quality line drawing / concept render — better input for Meshy.

    Accepts: multipart file field 'sketch_image'  OR  form field 'sketch_data' (base64)
    Optional: 'style' (line_art | clean_concept | detail_render), 'category', 'notes'
    Returns: { enhanced_url, original_url, style, notes }
    """
    style    = request.form.get('style', 'clean_concept')    # line_art | clean_concept | detail_render
    category = request.form.get('category', 'Jewellery').strip()
    notes    = request.form.get('notes', '').strip()

    # ── Resolve sketch image ──────────────────────────────────────────────────
    sketch_img = None
    has_sketch = False
    original_b64 = None

    sketch_file = request.files.get('sketch_image')
    if sketch_file and sketch_file.filename:
        try:
            raw = sketch_file.read()
            sketch_img = Image.open(io.BytesIO(raw)).convert('RGB')
            original_b64 = 'data:image/png;base64,' + base64.b64encode(raw).decode()
            has_sketch = True
        except Exception as e:
            log.warning(f'[enhance-sketch] could not read file: {e}')

    if not has_sketch:
        sketch_data = request.form.get('sketch_data', '')
        if sketch_data:
            try:
                b64 = sketch_data.split(',', 1)[1] if ',' in sketch_data else sketch_data
                raw = base64.b64decode(b64)
                sketch_img = Image.open(io.BytesIO(raw)).convert('RGB')
                original_b64 = sketch_data if sketch_data.startswith('data:') else ('data:image/png;base64,' + b64)
                has_sketch = True
            except Exception as e:
                log.warning(f'[enhance-sketch] could not decode sketch_data: {e}')

    if not has_sketch or sketch_img is None:
        return jsonify({'error': 'No sketch image provided'}), 400

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    style_instructions = {
        'line_art': (
            "Convert this rough sketch into a precise, clean technical line drawing. "
            "Use crisp black lines on a pure white background. "
            "Preserve all design intent — shapes, proportions, stone placements, decorative motifs — "
            "but clean up stray marks, wobbly lines, and smudges. "
            "The result should look like a drafter's clean ink drawing suitable for a goldsmith."
        ),
        'clean_concept': (
            "Transform this rough jewellery sketch into a polished concept illustration. "
            "Keep the overall design and proportions faithful to the original. "
            "Add subtle shading to suggest metal surfaces and stone facets. "
            "Clean white/cream background, professional jewellery illustration style. "
            "Make it suitable as a clear briefing document for a CAD operator."
        ),
        'detail_render': (
            "Enhance this jewellery sketch into a detailed photorealistic render while faithfully "
            "preserving the original design's shape, proportions, and all decorative elements. "
            "Show realistic gold/metal texture, stone settings, and surface finish. "
            "Professional product photography style on a clean background. "
            "This will be used as the input image for Meshy 3D generation — maximum clarity and detail."
        ),
    }.get(style, 'clean_concept')

    prompt = f"""You are a senior jewellery design illustrator.

The user has provided a rough sketch of a {category} jewellery piece{(' — notes: ' + notes) if notes else ''}.

{style_instructions}

IMPORTANT CONSTRAINTS:
- Preserve every design element from the original sketch exactly
- Do NOT add new design features that aren't in the sketch
- Do NOT add a model or body part; show only the jewellery piece itself
- Maintain the same viewing angle/perspective as the original
- Output a single, complete jewellery illustration on a clean background"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash-image',
            contents=[prompt, sketch_img],
            config=types.GenerateContentConfig(response_modalities=['TEXT', 'IMAGE']),
        )

        enhanced_url = None
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                out_bytes = part.inline_data.data
                out_fn    = f'enhanced_{uuid.uuid4()}.png'
                r2_url    = _upload_to_r2(out_bytes, f'enhanced/{out_fn}')
                if r2_url:
                    enhanced_url = r2_url
                else:
                    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                    Image.open(io.BytesIO(out_bytes)).save(out_path)
                    enhanced_url = f'/static/outputs/{out_fn}'
                break

        if not enhanced_url:
            text_parts = [p.text for p in response.candidates[0].content.parts if p.text]
            return jsonify({'error': 'Gemini returned no image.', 'details': ' '.join(text_parts)}), 500

        log.info(f'[enhance-sketch] done | style={style} | url={enhanced_url}')
        return jsonify({'success': True, 'enhanced_url': enhanced_url, 'original_url': original_b64, 'style': style})

    except Exception as e:
        log.error(f'[enhance-sketch] exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── API: CAD Comparison (2D sketch vs generated 3D) ───────────────────────────

@app.route('/api/compare-cad', methods=['POST'])
@login_required
def api_compare_cad():
    """
    Side-by-side AI comparison of a 2D source image vs the generated 3D thumbnail.

    Body (JSON or form):
      sketch_data   – base64 data-URI of the original 2D image
      thumbnail_url – URL of the Meshy 3D thumbnail (or any generated 3D preview)
      category      – jewellery category string

    Returns structured JSON:
      {
        "similarity_score": 0-100,
        "design_preserved": ["element1", ...],
        "design_lost":      ["element2", ...],
        "design_changed":   ["element3", ...],
        "verdict":          "PASS | WARN | FAIL",
        "summary":          "2-3 sentence plain-English comparison",
        "recommendations":  ["fix 1", ...]
      }
    """
    data          = request.json or request.form
    sketch_data   = (data.get('sketch_data') or '').strip()
    thumbnail_url = (data.get('thumbnail_url') or '').strip()
    category      = (data.get('category') or 'Jewellery').strip()

    if not sketch_data:
        return jsonify({'error': 'sketch_data (base64) required'}), 400
    if not thumbnail_url:
        return jsonify({'error': 'thumbnail_url required'}), 400

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    # Cache key
    cache_key = 'glymr:compare:' + hashlib.sha1(
        (sketch_data[:200] + thumbnail_url + category).encode()
    ).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        # Decode source sketch
        b64 = sketch_data.split(',', 1)[1] if ',' in sketch_data else sketch_data
        source_img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGB')
    except Exception as e:
        return jsonify({'error': f'Could not decode sketch_data: {e}'}), 400

    try:
        # Fetch 3D thumbnail
        if thumbnail_url.startswith('http://') or thumbnail_url.startswith('https://'):
            r = requests.get(thumbnail_url, timeout=15)
            r.raise_for_status()
            thumb_img = Image.open(io.BytesIO(r.content)).convert('RGB')
        else:
            fn = thumbnail_url.split('/static/outputs/')[-1].split('?')[0]
            thumb_img = Image.open(os.path.join(app.config['OUTPUT_FOLDER'], fn)).convert('RGB')
    except Exception as e:
        return jsonify({'error': f'Could not fetch thumbnail: {e}'}), 400

    def pil_to_part(img):
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return types.Part.from_bytes(data=buf.getvalue(), mime_type='image/png')

    prompt = f"""You are a senior jewellery CAD quality-control specialist.

Image 1 (SOURCE): The original 2D design — either a hand sketch, concept render, or product photo.
Image 2 (OUTPUT): A 3D model render generated by Meshy AI from Image 1.

Category: {category}

Your task is to compare Image 1 and Image 2 and assess how faithfully the 3D model
captured the original design intent.

Analyse across these dimensions:
- Overall silhouette and proportions
- Individual design elements (motifs, filigree, pendants, stones, settings)
- Symmetry and balance
- Surface texture and finish representation
- Stone placement and count
- Structural features (shanks, clasps, links, prongs)

Return ONLY this exact JSON, no markdown:
{{
  "similarity_score": <integer 0-100>,
  "verdict": "<PASS|WARN|FAIL>",
  "summary": "<2-3 sentence plain-English comparison verdict>",
  "design_preserved": ["<element accurately captured>", ...],
  "design_lost": ["<element missing or absent in 3D>", ...],
  "design_changed": ["<element present but distorted or altered>", ...],
  "recommendations": ["<actionable fix to improve fidelity>", ...]
}}

SCORING GUIDE: 80-100=high fidelity (PASS), 55-79=moderate (WARN), <55=low fidelity (FAIL).
Be specific — reference actual visible features, not generic boilerplate."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, pil_to_part(source_img), pil_to_part(thumb_img)],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        raw   = response.text.strip().replace('```json', '').replace('```', '').strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return jsonify({'error': 'No JSON in response. Raw: ' + raw[:200]}), 500
        parsed = json.loads(match.group(0))
        result = {'success': True, **parsed}
        cache_set(cache_key, result, ttl=1800)
        log.info(f'[compare-cad] done | category={category} | score={parsed.get("similarity_score")}')
        return jsonify(result)
    except Exception as e:
        log.error(f'[compare-cad] exception: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Design Feature Extraction (structured sketch analysis) ───────────────

@app.route('/api/extract-design-features', methods=['POST'])
@login_required
def api_extract_design_features():
    """
    Run structured feature extraction on a 2D jewellery sketch/image before
    sending to Meshy, so we can (a) show the user what was recognised and
    (b) enrich the Meshy object_prompt with precise geometry hints.

    Accepts: multipart file 'image' OR form field 'image_data' (base64 data-URI)
    Optional: 'category'

    Returns:
    {
      "success": true,
      "category": "Ring",
      "stone_count": 3,
      "stone_type": "round brilliant",
      "symmetry": "bilateral",
      "metal_finish": "polished yellow gold",
      "motifs": ["filigree", "floral"],
      "setting_type": "prong",
      "prong_count": 4,
      "shank_style": "split shank",
      "sketch_quality": "clear",
      "geometry_hint": "<concise sentence for Meshy object_prompt>",
      "warnings": ["thin prong detected", ...]
    }
    """
    category = request.form.get('category', 'Jewellery').strip()

    # Resolve image bytes
    img_bytes = None
    uploaded  = request.files.get('image')
    if uploaded and uploaded.filename:
        img_bytes = uploaded.read()
    else:
        image_data = request.form.get('image_data', '')
        if image_data:
            try:
                b64 = image_data.split(',', 1)[1] if ',' in image_data else image_data
                img_bytes = base64.b64decode(b64)
            except Exception as e:
                return jsonify({'success': False, 'error': f'Invalid base64: {e}'}), 400

    if not img_bytes:
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    client, err = get_gemini_client()
    if err:
        return jsonify({'success': False, 'error': err}), 500

    # Cache to avoid re-running on repeated generate attempts with the same image
    cache_key = 'glymr:features:' + hashlib.sha1(img_bytes + category.encode()).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        log.info('[extract-features] cache HIT')
        return jsonify(cached)

    # ── Category-specific geometry prompt guidance ───────────────────────────
    _CATEGORY_GEOMETRY_GUIDE = {
        'Ring': (
            'Focus on: shank profile (round/half-round/flat/comfort-fit), '
            'shank width & thickness, head/gallery style, stone seat depth, '
            'prong geometry (round/claw/double-claw/bezel), shoulder design, '
            'and inner bore diameter. Describe how the shank transitions to the head.'
        ),
        'Necklace': (
            'Focus on: chain link style (cable/box/rope/snake/figaro), '
            'pendant geometry, bail shape & connection, clasp type, '
            'overall length silhouette, stone suspension mechanism, '
            'and whether elements are layered or flat.'
        ),
        'Earrings': (
            'Focus on: post/hook/hoop geometry, ear wire curve, '
            'drop length, number of tiers, connection joints between elements, '
            'stone setting orientation, and overall front-facing silhouette.'
        ),
        'Bracelet': (
            'Focus on: link geometry, clasp/closure type, '
            'overall band width & thickness, stone setting rows, '
            'hinge positions if any, and how the piece would curve around a wrist.'
        ),
        'Brooch': (
            'Focus on: pin-back placement, overall silhouette depth, '
            'stone cluster arrangement, filigree or wire-work framing, '
            'and whether the piece has multiple depth layers.'
        ),
    }
    cat_guide = _CATEGORY_GEOMETRY_GUIDE.get(category, (
        'Describe the 3D form comprehensively: overall silhouette, depth, '
        'connection points, stone placement, surface texture, and structural joints.'
    ))

    prompt = f"""You are an expert jewellery CAD analyst.

Examine this {category} jewellery image (sketch, render, or photo) and extract every observable design detail.

CATEGORY-SPECIFIC GEOMETRY FOCUS FOR {category.upper()}:
{cat_guide}

Return ONLY this exact JSON, no markdown, no extra keys:
{{
  "category": "<detected jewellery type, e.g. Ring / Necklace / Earring / Bracelet>",
  "stone_count": <integer or null if no stones visible>,
  "stone_type": "<e.g. round brilliant / oval / marquise / no stones>",
  "stone_size_hint": "<e.g. large centre stone + 6 side stones / uniform small stones>",
  "symmetry": "<bilateral / radial / asymmetric / unknown>",
  "metal_finish": "<e.g. high-polish yellow gold / matte white gold / textured rose gold>",
  "motifs": ["<motif1>", "<motif2>"],
  "setting_type": "<prong / bezel / pavé / channel / invisible / none>",
  "prong_count": <integer or null>,
  "shank_style": "<e.g. plain band / split shank / twisted / tapered / N/A>",
  "sketch_quality": "<clear / moderate / rough>",
  "sketch_quality_reason": "<one sentence explaining the quality rating>",
  "enhance_recommended": <true if sketch_quality is 'rough' or background is cluttered, else false>,
  "geometry_hint": "<ONE concise sentence describing the 3D form, incorporating the category-specific focus above. Max 40 words. E.g. for a ring: 'Bilateral-symmetry ring with large oval centre stone in 4-prong head on a tapered split shank, pavé stones along shoulders, half-round comfort-fit band profile.'>",
  "warnings": ["<any fragility / manufacturability concern visible in the sketch>"]
}}

RULES:
- stone_count: count only the clearly distinct stones, not implied ones.
- geometry_hint: must be ≤40 words, specific, spatial — this is injected directly into the 3D generation prompt. Prioritise the category-specific focus points above.
- enhance_recommended: set true whenever clarity would meaningfully improve 3D reconstruction.
- warnings: list only issues actually visible; use empty array [] if none.
- If a field cannot be determined from the image, use null."""

    try:
        pil = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, pil],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        raw   = response.text.strip().replace('```json', '').replace('```', '').strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return jsonify({'success': False, 'error': 'No JSON in response. Raw: ' + raw[:200]}), 500
        parsed = json.loads(match.group(0))
        result = {'success': True, **parsed}
        cache_set(cache_key, result, ttl=3600)
        log.info(f'[extract-features] done | category={parsed.get("category")} | stones={parsed.get("stone_count")} | quality={parsed.get("sketch_quality")} | enhance={parsed.get("enhance_recommended")}')
        return jsonify(result)
    except Exception as e:
        log.error(f'[extract-features] exception: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: STEP/IGES conversion (OBJ → STEP or IGES via trimesh + pythonOCC) ────

@app.route('/api/convert-to-step', methods=['POST'])
@login_required
def api_convert_to_step():
    """
    Download an OBJ from Meshy and convert it to STEP (preferred) or IGES.

    Strategy (in priority order):
      1. pythonOCC (pip install pythonocc-core) — full BREP / topology repair
      2. trimesh + numpy-stl — triangulated STEP shell (no topology, but valid)
      3. Raw OBJ re-packed as ASCII IGES mesh (always available, no deps)

    Body JSON:
        { "obj_url": "https://…/model.obj",
          "title": "optional",
          "format": "step" | "iges"   (default "step") }

    Returns:
        { "success": true, "step_url": "…", "filename": "…",
          "method": "pythonocc|trimesh|iges_fallback",
          "note": "…" }
    """
    body       = request.json or {}
    obj_url    = body.get('obj_url', '').strip()
    title      = body.get('title', 'jewellery_model').strip() or 'jewellery_model'
    fmt        = body.get('format', 'step').lower().strip()
    if fmt not in ('step', 'iges'):
        fmt = 'step'

    if not obj_url:
        return jsonify({'success': False, 'error': 'obj_url required'}), 400

    from urllib.parse import urlparse
    host = urlparse(obj_url).hostname or ''
    if not (host.endswith('meshy.ai') or host.endswith('aliyuncs.com')):
        return jsonify({'success': False, 'error': 'URL not from an allowed domain'}), 403

    try:
        r = requests.get(obj_url, timeout=90)
        r.raise_for_status()
        obj_bytes = r.content
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not download OBJ: {e}'}), 502

    tmp_dir  = app.config['OUTPUT_FOLDER']
    safe     = re.sub(r'[^a-zA-Z0-9_-]', '_', title)[:40]
    uid_hex  = uuid.uuid4().hex[:8]
    obj_path = os.path.join(tmp_dir, f'{safe}_{uid_hex}.obj')
    ext      = 'iges' if fmt == 'iges' else 'step'
    out_fn   = f'{safe}_{uid_hex}.{ext}'
    out_path = os.path.join(tmp_dir, out_fn)

    with open(obj_path, 'wb') as fh:
        fh.write(obj_bytes)

    method     = None
    out_bytes  = None

    # ── Strategy 1: pythonOCC (full BREP, best quality) ──────────────────────
    try:
        from OCC.Core.BRep        import BRep_Builder
        from OCC.Core.BRepMesh    import BRepMesh_IncrementalMesh
        from OCC.Core.TopoDS      import TopoDS_Compound
        from OCC.Core.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCC.Core.IGESControl import IGESControl_Writer
        from OCC.Core.IFSelect    import IFSelect_RetDone
        import trimesh as _tm

        mesh = _tm.load(obj_path, force='mesh')
        if not hasattr(mesh, 'faces'):
            raise ValueError('trimesh load produced no faces')

        builder  = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)

        from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace
        from OCC.Core.gp            import gp_Pnt

        verts = mesh.vertices
        for face in mesh.faces:
            try:
                poly = BRepBuilderAPI_MakePolygon()
                for vi in face:
                    v = verts[vi]
                    poly.Add(gp_Pnt(float(v[0]), float(v[1]), float(v[2])))
                poly.Close()
                wire  = poly.Wire()
                bface = BRepBuilderAPI_MakeFace(wire)
                if bface.IsDone():
                    builder.Add(compound, bface.Face())
            except Exception:
                pass

        if fmt == 'iges':
            writer = IGESControl_Writer()
            writer.AddShape(compound)
            writer.ComputeModel()
            writer.Write(out_path)
        else:
            writer = STEPControl_Writer()
            writer.Transfer(compound, STEPControl_AsIs)
            status = writer.Write(out_path)
            if status != IFSelect_RetDone:
                raise RuntimeError('STEP write failed')

        with open(out_path, 'rb') as fh:
            out_bytes = fh.read()
        method = 'pythonocc'
        log.info(f'[convert-step] pythonOCC success | fmt={fmt}')

    except ImportError:
        log.info('[convert-step] pythonOCC not available, trying trimesh fallback')
    except Exception as e:
        log.warning(f'[convert-step] pythonOCC failed: {e}')

    # ── Strategy 2: trimesh → STEP shell (triangulated, universally readable) ─
    if out_bytes is None:
        try:
            import trimesh as _tm
            import numpy as _np

            mesh = _tm.load(obj_path, force='mesh')
            if not hasattr(mesh, 'faces') or len(mesh.faces) == 0:
                raise ValueError('empty mesh')

            verts  = mesh.vertices
            faces  = mesh.faces
            nv     = len(verts)
            nf     = len(faces)

            # Build a minimal ASCII STEP AP203 with triangulated faces
            lines = [
                "ISO-10303-21;",
                "HEADER;",
                "FILE_DESCRIPTION(('Jewellery model exported by glymr'),'2;1');",
                f"FILE_NAME('{safe}.step','',('glymr'),(''),'',' ','');",
                "FILE_SCHEMA(('AP203_CONFIGURATION_CONTROLLED_3D_DESIGN_OF_MECHANICAL_PARTS_AND_ASSEMBLIES_MIM_LF { 1 0 10303 403 1 1 4 }'));",
                "ENDSEC;",
                "DATA;",
            ]
            idx = 1
            cart_ids   = []
            vertex_ids = []
            for v in verts:
                lines.append(f"#{idx}=CARTESIAN_POINT('',(  {v[0]:.6f},  {v[1]:.6f},  {v[2]:.6f}));")
                cart_ids.append(idx); idx += 1
                lines.append(f"#{idx}=VERTEX_POINT('',#{idx-1});")
                vertex_ids.append(idx); idx += 1

            face_ids = []
            for f in faces:
                vi = [vertex_ids[f[0]], vertex_ids[f[1]], vertex_ids[f[2]]]
                e_ids = []
                for a, b in [(vi[0], vi[1]), (vi[1], vi[2]), (vi[2], vi[0])]:
                    lines.append(f"#{idx}=EDGE_CURVE('',#{a},#{b},LINE('',CARTESIAN_POINT('',(0.,0.,0.)),VECTOR('',DIRECTION('',(1.,0.,0.)),1.)),.F.);")
                    e_ids.append(idx); idx += 1
                    lines.append(f"#{idx}=ORIENTED_EDGE('',*,*,#{idx-1},.T.);")
                    idx += 1
                oe = [idx - 5, idx - 3, idx - 1]  # oriented edges
                lines.append(f"#{idx}=EDGE_LOOP('',({','.join('#'+str(o) for o in oe)}));")
                loop_id = idx; idx += 1
                lines.append(f"#{idx}=FACE_OUTER_BOUND('',#{loop_id},.T.);")
                bound_id = idx; idx += 1
                lines.append(f"#{idx}=ADVANCED_FACE('',(#{bound_id}),PLANE('',AXIS2_PLACEMENT_3D('',CARTESIAN_POINT('',(0.,0.,0.)),DIRECTION('',(0.,0.,1.)),DIRECTION('',(1.,0.,0.)))),.T.);")
                face_ids.append(idx); idx += 1

            face_list = ','.join(f'#{fi}' for fi in face_ids)
            lines.append(f"#{idx}=CLOSED_SHELL('',({face_list}));")
            shell_id = idx; idx += 1
            lines.append(f"#{idx}=MANIFOLD_SOLID_BREP('jewellery',#{shell_id});")
            msb_id = idx; idx += 1
            lines.append(f"#{idx}=SHAPE_REPRESENTATION('',(#{msb_id}),( #{ idx+1 } ));")
            sr_id = idx; idx += 1
            lines.append(f"#{idx}=( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{idx+1})) GLOBAL_UNIT_ASSIGNED_CONTEXT((#{idx+2},#{idx+3},#{idx+4})) REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY') );")
            ctx_id = idx; idx += 1
            lines += [
                f"#{idx}=UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{idx+1},'distance_accuracy_value','Confusion accuracy');",
                f"#{idx+1}=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));",
                f"#{idx+2}=(NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.));",
                f"#{idx+3}=(NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT());",
            ]
            lines += ["ENDSEC;", "END-ISO-10303-21;"]

            out_bytes = '\n'.join(lines).encode('utf-8')
            out_fn    = out_fn.replace('.iges', '.step').replace(f'.{ext}', '.step')
            ext       = 'step'
            out_path  = os.path.join(tmp_dir, out_fn)
            with open(out_path, 'wb') as fh:
                fh.write(out_bytes)
            method = 'trimesh'
            log.info(f'[convert-step] trimesh STEP shell | faces={nf}')

        except ImportError:
            log.info('[convert-step] trimesh not available, using IGES plain-mesh fallback')
        except Exception as e:
            log.warning(f'[convert-step] trimesh STEP failed: {e}')

    # ── Strategy 3: Minimal ASCII IGES (pure Python, always works) ───────────
    if out_bytes is None:
        try:
            iges_lines = _obj_to_iges_ascii(obj_bytes.decode('utf-8', errors='replace'), safe)
            out_fn    = out_fn.replace('.step', '.iges')
            ext       = 'iges'
            out_path  = os.path.join(tmp_dir, out_fn)
            out_bytes = iges_lines.encode('ascii', errors='replace')
            with open(out_path, 'wb') as fh:
                fh.write(out_bytes)
            method = 'iges_fallback'
            log.info('[convert-step] plain IGES fallback produced')
        except Exception as e:
            log.error(f'[convert-step] all strategies failed: {e}', exc_info=True)
            return jsonify({'success': False, 'error': 'All conversion strategies failed. '
                            'Download the OBJ/GLB and import into Rhino or Fusion 360.'}), 500

    # Clean up temp OBJ
    try:
        os.unlink(obj_path)
    except Exception:
        pass

    r2_url  = _upload_to_r2(out_bytes, f'outputs/{out_fn}')
    out_url = r2_url if r2_url else f'/static/outputs/{out_fn}'

    note_map = {
        'pythonocc':     'Full BREP STEP — importable in Rhino, Fusion 360, SolidWorks, FreeCAD.',
        'trimesh':       'Triangulated STEP shell — valid in all CAD tools; use Fusion 360 to heal if needed.',
        'iges_fallback': 'IGES mesh export — open in Rhino or FreeCAD for further editing.',
    }
    log.info(f'[convert-step] done | method={method} | url={out_url}')
    return jsonify({
        'success':  True,
        'step_url': out_url,
        'filename': out_fn,
        'format':   ext,
        'method':   method,
        'note':     note_map.get(method, ''),
    })


# ── API: Mesh-based physical analysis ─────────────────────────────────────────

@app.route('/api/analyse-mesh', methods=['POST'])
@login_required
def api_analyse_mesh():
    """
    Download an OBJ/GLB from Meshy and run geometric analysis via trimesh:
      - Volume and estimated metal weight (g)
      - Surface area (cm²)
      - Bounding box dimensions (mm)
      - Wall-thickness sampling (min/mean/percentile)
      - Minimum feature size (proxy: shortest edge in mesh)
      - Watertight / manifold check
      - Centre of mass

    Body JSON:
        { "obj_url": "https://…/model.obj",
          "metal": "22K Yellow Gold",          (optional, for weight calc)
          "category": "Ring"                   (optional, for context)
        }

    Returns structured JSON suitable for display and for enriching the BOM.
    """
    body     = request.json or {}
    obj_url  = body.get('obj_url', '').strip()
    metal    = body.get('metal', '22K Yellow Gold').strip()
    category = body.get('category', 'Jewellery').strip()

    if not obj_url:
        return jsonify({'success': False, 'error': 'obj_url required'}), 400

    from urllib.parse import urlparse as _urlparse
    host = _urlparse(obj_url).hostname or ''
    if not (host.endswith('meshy.ai') or host.endswith('aliyuncs.com')):
        return jsonify({'success': False, 'error': 'URL not from an allowed domain'}), 403

    try:
        r = requests.get(obj_url, timeout=90)
        r.raise_for_status()
        obj_bytes = r.content
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not download mesh: {e}'}), 502

    try:
        import trimesh as _tm
        import numpy as _np
    except ImportError:
        return jsonify({'success': False, 'error': 'trimesh not installed — add trimesh>=4.3.0 and numpy>=1.26.0 to requirements.txt'}), 500

    try:
        # Load from bytes
        mesh = _tm.load(
            _tm.util.wrap_as_stream(obj_bytes),
            file_type='obj',
            force='mesh',
        )
        # Some OBJ files come back as a Scene; merge to single mesh
        if isinstance(mesh, _tm.Scene):
            if mesh.geometry:
                mesh = _tm.util.concatenate(list(mesh.geometry.values()))
            else:
                return jsonify({'success': False, 'error': 'Mesh scene contained no geometry.'}), 422

        if not hasattr(mesh, 'faces') or len(mesh.faces) == 0:
            return jsonify({'success': False, 'error': 'Loaded mesh has no faces.'}), 422

        # ── Basic geometry ─────────────────────────────────────────────────────
        # Meshy outputs in metres; convert to mm for jewellery context
        M_TO_MM = 1000.0
        vol_m3      = float(mesh.volume)           # signed; abs for hollow
        vol_cm3     = abs(vol_m3) * 1e6            # cm³
        area_m2     = float(mesh.area)
        area_cm2    = area_m2 * 1e4                # cm²

        # Bounding box in mm
        extents_mm  = mesh.bounding_box.extents * M_TO_MM
        bbox = {
            'x_mm': round(float(extents_mm[0]), 2),
            'y_mm': round(float(extents_mm[1]), 2),
            'z_mm': round(float(extents_mm[2]), 2),
        }

        # ── Weight estimate from metal density ────────────────────────────────
        density_g_cm3 = _METAL_DENSITY.get(metal, 10.5)
        # jewellery is not solid: typical casting factor ~0.75 (hollow + wax shrink)
        CASTING_FACTOR = 0.75
        est_weight_g   = round(vol_cm3 * density_g_cm3 * CASTING_FACTOR, 2)

        # ── Watertight / manifold check ───────────────────────────────────────
        is_watertight = bool(mesh.is_watertight)
        is_manifold   = bool(mesh.is_volume)   # volume ≠ 0 and watertight

        # ── Wall-thickness via ray sampling ──────────────────────────────────
        # Sample N points on the surface, cast rays inward; distance = local thickness
        thickness_samples = []
        thickness_warnings = []
        try:
            N_SAMPLES = 800
            pts, face_idx = _tm.sample.sample_surface(mesh, N_SAMPLES)
            normals = mesh.face_normals[face_idx]
            # Ray inward = flip normal
            ray_origins    = pts - normals * 1e-4   # offset slightly to avoid self-hit
            ray_directions = -normals
            locs, _, _ = mesh.ray.intersects_location(
                ray_origins=ray_origins,
                ray_directions=ray_directions,
                multiple_hits=False,
            )
            if len(locs) > 10:
                dists = _np.linalg.norm(locs - ray_origins[:len(locs)], axis=1) * M_TO_MM
                dists = dists[dists > 0.01]  # filter near-zero hits (self)
                thickness_samples = dists
                t_min  = round(float(_np.min(dists)),  2)
                t_mean = round(float(_np.mean(dists)), 2)
                t_p10  = round(float(_np.percentile(dists, 10)), 2)
                # Manufacturing thresholds (mm) per category
                MIN_WALL = {'Ring': 0.8, 'Necklace': 0.5, 'Earrings': 0.4}.get(category, 0.6)
                if t_min < MIN_WALL:
                    thickness_warnings.append(
                        f'Minimum wall thickness {t_min}mm is below the recommended {MIN_WALL}mm for {category}. '
                        'Risk of breakage during casting or wear.'
                    )
                if t_p10 < MIN_WALL * 1.5:
                    thickness_warnings.append(
                        f'10th-percentile thickness {t_p10}mm suggests thin sections. '
                        'Review prong tips or fine filigree before sending to manufacture.'
                    )
        except Exception as thick_err:
            log.warning(f'[analyse-mesh] thickness sampling failed: {thick_err}')
            t_min = t_mean = t_p10 = None

        # ── Minimum edge length (smallest feature proxy) ──────────────────────
        edges     = mesh.edges_unique
        verts     = mesh.vertices
        edge_vecs = verts[edges[:, 1]] - verts[edges[:, 0]]
        edge_lens = _np.linalg.norm(edge_vecs, axis=1) * M_TO_MM
        min_edge_mm = round(float(_np.min(edge_lens)), 3)
        if min_edge_mm < 0.1:
            thickness_warnings.append(
                f'Mesh contains edges as small as {min_edge_mm}mm — '
                'likely mesh artefacts or extreme filigree detail. Verify in CAD.'
            )

        # ── Centre of mass ────────────────────────────────────────────────────
        com = mesh.center_mass * M_TO_MM
        centre_of_mass = {'x': round(float(com[0]), 2), 'y': round(float(com[1]), 2), 'z': round(float(com[2]), 2)}

        # ── Overall manufacturability assessment ──────────────────────────────
        score = 100
        issues = []
        if not is_watertight:
            score -= 20
            issues.append({'check': 'Watertight mesh', 'status': 'FAIL',
                           'detail': 'Mesh has open edges — not suitable for casting without repair.'})
        if t_min is not None and t_min < {'Ring': 0.8, 'Necklace': 0.5, 'Earrings': 0.4}.get(category, 0.6):
            score -= 25
            issues.append({'check': 'Minimum wall thickness', 'status': 'FAIL',
                           'detail': f'Minimum wall {t_min}mm — too thin for reliable casting.'})
        elif t_min is not None:
            issues.append({'check': 'Minimum wall thickness', 'status': 'PASS',
                           'detail': f'Minimum wall {t_min}mm — within acceptable range.'})
        if is_watertight:
            issues.append({'check': 'Watertight mesh', 'status': 'PASS', 'detail': 'Mesh is watertight and manifold.'})

        if score >= 80:
            overall = 'PASS'
        elif score >= 55:
            overall = 'WARN'
        else:
            overall = 'FAIL'

        result = {
            'success': True,
            'mesh_stats': {
                'vertex_count':  len(mesh.vertices),
                'face_count':    len(mesh.faces),
                'volume_cm3':    round(vol_cm3, 4),
                'area_cm2':      round(area_cm2, 3),
                'bounding_box_mm': bbox,
                'is_watertight': is_watertight,
                'is_manifold':   is_manifold,
                'min_edge_mm':   min_edge_mm,
                'centre_of_mass_mm': centre_of_mass,
            },
            'thickness': {
                'min_mm':    t_min,
                'mean_mm':   t_mean,
                'p10_mm':    t_p10,
                'warnings':  thickness_warnings,
            },
            'weight': {
                'metal':          metal,
                'density_g_cm3':  density_g_cm3,
                'casting_factor': CASTING_FACTOR,
                'volume_cm3':     round(vol_cm3, 4),
                'est_weight_g':   est_weight_g,
                'note':           'Estimated from mesh volume × metal density × casting factor. Verify with actual casting.',
            },
            'manufacturability': {
                'score':   score,
                'overall': overall,
                'checks':  issues,
            },
        }
        log.info(
            f'[analyse-mesh] done | verts={len(mesh.vertices)} | vol={vol_cm3:.4f}cm³ | '
            f'weight~{est_weight_g}g | watertight={is_watertight} | verdict={overall}'
        )
        return jsonify(result)

    except Exception as e:
        log.error(f'[analyse-mesh] exception: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Background removal / foreground segmentation ─────────────────────────

@app.route('/api/remove-background', methods=['POST'])
@login_required
def api_remove_background():
    """
    Remove background from a jewellery image using Gemini's image editing
    capabilities (segmentation + inpainting to white background).
    Falls back to a simple white-threshold mask if Gemini returns no image.

    Accepts: multipart file 'image' OR form field 'image_data' (base64 data-URI)
    Optional: 'category' for context in the segmentation prompt

    Returns:
        { "success": true, "url": "…", "method": "gemini|threshold" }
    """
    category = request.form.get('category', 'Jewellery').strip()

    img_bytes = None
    uploaded  = request.files.get('image')
    if uploaded and uploaded.filename:
        img_bytes = uploaded.read()
    else:
        image_data = request.form.get('image_data', '')
        if image_data:
            try:
                b64 = image_data.split(',', 1)[1] if ',' in image_data else image_data
                img_bytes = base64.b64decode(b64)
            except Exception as e:
                return jsonify({'success': False, 'error': f'Invalid base64: {e}'}), 400

    if not img_bytes:
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    client, err = get_gemini_client()
    if err:
        return jsonify({'success': False, 'error': err}), 500

    try:
        pil_orig = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not decode image: {e}'}), 400

    prompt = (
        f'This image shows a {category} jewellery piece. '
        'Remove the background completely and replace it with a plain, pure white background (#FFFFFF). '
        'Keep every detail of the jewellery intact — do not blur or erase any part of the jewellery itself. '
        'The result should look like a professional product photograph on a white studio background.'
    )

    method = 'gemini'
    out_url = None

    # ── Attempt 1: Gemini image editing ──────────────────────────────────────
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash-image',
            contents=[prompt, pil_orig],
            config=types.GenerateContentConfig(response_modalities=['TEXT', 'IMAGE']),
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                out_bytes = part.inline_data.data
                out_fn    = f'bg_removed_{uuid.uuid4()}.png'
                r2_url    = _upload_to_r2(out_bytes, f'outputs/{out_fn}')
                if r2_url:
                    out_url = r2_url
                else:
                    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                    Image.open(io.BytesIO(out_bytes)).save(out_path)
                    out_url = f'/static/outputs/{out_fn}'
                break
    except Exception as e:
        log.warning(f'[remove-background] Gemini failed: {e}')

    # ── Attempt 2: PIL luminance threshold (always available, cruder) ────────
    if not out_url:
        method = 'threshold'
        try:
            import numpy as _np
            arr  = _np.array(pil_orig.convert('RGB'))
            # Create RGBA canvas with white background
            rgba = _np.ones((arr.shape[0], arr.shape[1], 4), dtype=_np.uint8) * 255
            rgba[:, :, :3] = arr
            # Simple mask: pixels close to white → transparent; rest → opaque
            # Works well for studio shots; rough for complex backgrounds
            gray = _np.mean(arr, axis=2)
            # Mark very bright near-white pixels as transparent
            mask = (gray > 240) & (_np.std(arr, axis=2) < 15)
            rgba[mask, 3] = 0   # transparent where background
            # Compose onto white background
            bg     = _np.ones_like(arr, dtype=_np.uint8) * 255
            alpha  = rgba[:, :, 3:4].astype(_np.float32) / 255.0
            result = (arr * alpha + bg * (1 - alpha)).astype(_np.uint8)
            out_img   = Image.fromarray(result)
            out_fn    = f'bg_removed_{uuid.uuid4()}.png'
            buf       = io.BytesIO()
            out_img.save(buf, format='PNG')
            r2_url    = _upload_to_r2(buf.getvalue(), f'outputs/{out_fn}')
            if r2_url:
                out_url = r2_url
            else:
                out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                out_img.save(out_path)
                out_url = f'/static/outputs/{out_fn}'
        except Exception as e:
            log.error(f'[remove-background] threshold fallback failed: {e}')
            return jsonify({'success': False, 'error': 'Background removal failed on both paths.'}), 500

    log.info(f'[remove-background] done | method={method} | url={out_url}')
    return jsonify({'success': True, 'url': out_url, 'method': method})


def _obj_to_iges_ascii(obj_text: str, name: str) -> str:
    """
    Convert OBJ text to a minimal IGES 5.3 ASCII file (entity type 308 group
    + 116 point entities).  Not a full BREP — but importable as a point cloud /
    mesh in most CAD applications and always producible with zero dependencies.
    """
    import math
    verts  = []
    for line in obj_text.splitlines():
        parts = line.strip().split()
        if parts and parts[0] == 'v':
            try:
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except (IndexError, ValueError):
                pass

    # IGES fixed-width 80-char line format
    def igs_line(section, seq, content):
        return f"{content:<72}{section}{seq:>7}"

    start = [igs_line('S', 1, f"{name[:72]}")]
    now   = datetime.now().strftime('%Y%m%d.%H%M%S')
    global_params = (
        f"1H,,1H;,{len(name)}H{name},{len(name)+8}H{name}.iges,"
        f"7Hglymr,7Hglymr,32,38,7,38,15,,1.0,2,2HMM,1,1.,{now},"
        f"1.0E-04,10.,1Hx,1H ,11,0,{now};"
    )
    g_lines = []
    for i in range(0, len(global_params), 72):
        g_lines.append(igs_line('G', i//72+1, global_params[i:i+72]))

    d_lines, p_lines = [], []
    for i, (x, y, z) in enumerate(verts[:5000], start=1):
        de_seq = 2*i - 1
        pd_seq = i
        d_lines.append(igs_line('D', de_seq,   f"      116{'':>9}{'':>9}{'':>9}{'':>9}{'':>9}0D{de_seq:>7}"))
        d_lines.append(igs_line('D', de_seq+1, f"      116     0     0     1     0{'':>26}0D{de_seq+1:>7}"))
        p_lines.append(igs_line('P', pd_seq,   f"116,{x:.6f},{y:.6f},{z:.6f};{' '*(40-len(str(de_seq)))}     {de_seq}P{pd_seq:>7}"))

    t_line = igs_line('T', 1,
        f"{'':>8}{len(start):>8}{len(g_lines):>8}{len(d_lines):>8}{len(p_lines):>8}{'':>40}")

    all_lines = start + g_lines + d_lines + p_lines + [t_line]
    return '\n'.join(all_lines) + '\n'


# ── API: Multi-angle CAD (accepts up to 4 images) ─────────────────────────────

@app.route('/api/generate-cad-multi', methods=['POST'])
@login_required
def api_generate_cad_multi():
    """
    Submit multiple angle images to Meshy image-to-3D for better reconstruction.
    Uses the first/primary image as the Meshy image_url.
    Additional angles are composited into a 2×2 grid and uploaded as a single image,
    which Meshy uses via its object_prompt context hint.

    Accepts: multipart files  image_0 (primary), image_1, image_2, image_3  (up to 4)
    Falls back to /api/generate-cad behaviour when only one image provided.
    """
    prompt        = request.form.get('prompt', '').strip()
    art_style     = request.form.get('art_style', 'realistic')
    target_use    = request.form.get('target_use', 'visualization')
    geometry_hint = request.form.get('geometry_hint', '').strip()
    images_raw = []
    for i in range(4):
        f = request.files.get(f'image_{i}')
        if f and f.filename:
            try:
                images_raw.append(f.read())
            except Exception:
                pass

    if not images_raw:
        return jsonify({'error': 'No images provided'}), 400

    try:
        # ── Primary image — always sent as Meshy's image_url ─────────────────
        pil_primary = Image.open(io.BytesIO(images_raw[0])).convert('RGBA')
        buf_primary = io.BytesIO()
        pil_primary.save(buf_primary, format='PNG')
        primary_bytes = buf_primary.getvalue()

        # Compress primary
        pil_rgb = pil_primary.convert('RGB')
        MAX_DIM  = 1200
        w, h = pil_rgb.size
        if w > MAX_DIM or h > MAX_DIM:
            ratio = min(MAX_DIM / w, MAX_DIM / h)
            pil_rgb = pil_rgb.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf_jpg = io.BytesIO()
        pil_rgb.save(buf_jpg, format='JPEG', quality=88)
        primary_compressed = buf_jpg.getvalue()

        # ── If multiple images: create a composite grid for context ──────────
        angle_note = ''
        if len(images_raw) > 1:
            imgs = []
            for raw in images_raw[:4]:
                try:
                    img = Image.open(io.BytesIO(raw)).convert('RGB').resize((512, 512), Image.LANCZOS)
                    imgs.append(img)
                except Exception:
                    pass

            if len(imgs) >= 2:
                cols = 2
                rows = (len(imgs) + 1) // 2
                grid = Image.new('RGB', (cols * 512, rows * 512), (255, 255, 255))
                for idx, img in enumerate(imgs):
                    grid.paste(img, ((idx % cols) * 512, (idx // cols) * 512))

                buf_grid = io.BytesIO()
                grid.save(buf_grid, format='JPEG', quality=85)
                grid_bytes = buf_grid.getvalue()
                grid_fn    = f'cad_grid_{uuid.uuid4()}.jpg'
                # Upload grid to R2 for object_prompt context (or ignore if unavailable)
                grid_url = _upload_to_r2(grid_bytes, f'cad-inputs/{grid_fn}')
                angle_count = len(imgs)
                angle_note = (
                    f' Multi-angle input: {angle_count} views provided '
                    f'(front, side, back, detail). Reconstruct all sides accurately.'
                )
                log.info(f'[cad-multi] grid uploaded: {grid_url} | angles={angle_count}')

        # ── Resolve public URL for primary image ──────────────────────────────
        tmp_fn = f'cad_input_{uuid.uuid4()}.jpg'
        public_img_url = _upload_to_r2(primary_compressed, f'cad-inputs/{tmp_fn}')
        if not public_img_url:
            tmp_path = os.path.join(app.config['UPLOAD_FOLDER'], tmp_fn)
            with open(tmp_path, 'wb') as fh:
                fh.write(primary_compressed)
            host = request.host_url.rstrip('/')
            if 'localhost' not in host and '127.0.0.1' not in host:
                public_img_url = f'{host}/static/uploads/{tmp_fn}'

        # ── Build Meshy payload ───────────────────────────────────────────────
        if public_img_url:
            payload = {'image_url': public_img_url, 'enable_pbr': True, 'should_remesh': True}
        else:
            b64str = base64.b64encode(primary_compressed).decode()
            payload = {'image_url': f'data:image/jpeg;base64,{b64str}', 'enable_pbr': True, 'should_remesh': True}

        # Compose richest possible object_prompt: geometry_hint + user prompt + angle note
        full_prompt_parts = []
        if geometry_hint:
            full_prompt_parts.append(geometry_hint)
        if prompt:
            full_prompt_parts.append(prompt)
        if angle_note:
            full_prompt_parts.append(angle_note.strip())
        full_prompt = ' '.join(full_prompt_parts).strip()
        if full_prompt:
            payload['object_prompt'] = full_prompt[:500]

        headers = {'Authorization': f'Bearer {meshy_key}', 'Content-Type': 'application/json'}
        log.info(f'[cad-multi] submitting | angles={len(images_raw)} | prompt={full_prompt[:80]}')
        res = requests.post(
            'https://api.meshy.ai/openapi/v1/image-to-3d',
            headers=headers, json=payload, timeout=30,
        )
        if res.status_code == 202:
            task_id = res.json().get('result')
            log.info(f'[cad-multi] task submitted | task_id={task_id}')
            return jsonify({'task_id': task_id, 'success': True, 'angle_count': len(images_raw)})
        else:
            log.error(f'[cad-multi] Meshy error {res.status_code}: {res.text}')
            return jsonify({'error': f'Meshy API error: {res.status_code}', 'details': res.text[:300]}), 500

    except Exception as e:
        log.error(f'[cad-multi] exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── API: Multi-angle CAD — Meshy v2 multi-view (turntable) ───────────────────

@app.route('/api/generate-cad-turntable', methods=['POST'])
@login_required
@rate_limited
def api_generate_cad_turntable():
    """
    Submit 2-6 turntable / multi-view images to Meshy image-to-3D v2
    using the `reference_image_urls` parameter so each angle is treated as a
    distinct viewpoint rather than a single blended grid.

    Accepts: multipart files  image_0 (front / primary), image_1 … image_5
    Optional form fields: prompt, geometry_hint, art_style, target_use

    Returns: { "task_id": "…", "success": true, "angle_count": N }
    """
    meshy_key, err = get_meshy_key()
    if err:
        return jsonify({'error': err}), 500

    prompt        = request.form.get('prompt', '').strip()
    geometry_hint = request.form.get('geometry_hint', '').strip()
    art_style     = request.form.get('art_style', 'realistic')
    target_use    = request.form.get('target_use', 'visualization')

    images_raw = []
    for i in range(6):
        f = request.files.get(f'image_{i}')
        if f and f.filename:
            try:
                images_raw.append(f.read())
            except Exception:
                pass

    if not images_raw:
        return jsonify({'error': 'No images provided'}), 400

    try:
        # ── Upload each angle to R2 (or fallback to base64 data-URIs) ─────────
        angle_urls = []
        for idx, raw in enumerate(images_raw[:6]):
            pil = Image.open(io.BytesIO(raw)).convert('RGB')
            MAX_DIM = 1024
            w, h = pil.size
            if w > MAX_DIM or h > MAX_DIM:
                ratio = min(MAX_DIM / w, MAX_DIM / h)
                pil   = pil.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            pil.save(buf, format='JPEG', quality=88)
            jpg = buf.getvalue()

            fn     = f'turntable_{uuid.uuid4().hex[:8]}_{idx}.jpg'
            r2_url = _upload_to_r2(jpg, f'cad-inputs/{fn}')
            if r2_url:
                angle_urls.append(r2_url)
            else:
                # Fallback: base64 data-URI (Meshy accepts these too)
                b64 = base64.b64encode(jpg).decode()
                angle_urls.append(f'data:image/jpeg;base64,{b64}')

        primary_url = angle_urls[0]
        ref_urls    = angle_urls[1:]  # additional viewpoints

        # ── Build object_prompt ───────────────────────────────────────────────
        parts = []
        if geometry_hint: parts.append(geometry_hint)
        if prompt:        parts.append(prompt)
        n = len(images_raw)
        parts.append(
            f'Multi-view turntable input: {n} viewpoints '
            f'(0°, {", ".join(str(round(i*360/n))+"°" for i in range(1,n))}). '
            'Reconstruct all sides with maximum geometric fidelity.'
        )
        object_prompt = ' '.join(parts).strip()[:500]

        payload = {
            'image_url':            primary_url,
            'enable_pbr':           True,
            'should_remesh':        True,
            'object_prompt':        object_prompt,
        }
        # Meshy v2 multi-view field — ignored gracefully on older API versions
        if ref_urls:
            payload['reference_image_urls'] = ref_urls

        headers = {
            'Authorization': f'Bearer {meshy_key}',
            'Content-Type':  'application/json',
        }
        log.info(f'[cad-turntable] submitting | angles={len(images_raw)} | prompt={object_prompt[:80]}')
        res = requests.post(
            'https://api.meshy.ai/openapi/v1/image-to-3d',
            headers=headers, json=payload, timeout=30,
        )
        if res.status_code == 202:
            task_id = res.json().get('result')
            log.info(f'[cad-turntable] task submitted | task_id={task_id}')
            return jsonify({'task_id': task_id, 'success': True, 'angle_count': len(images_raw)})
        else:
            log.error(f'[cad-turntable] Meshy error {res.status_code}: {res.text}')
            return jsonify({'error': f'Meshy API error: {res.status_code}', 'details': res.text[:300]}), 500

    except Exception as e:
        log.error(f'[cad-turntable] exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500



# ── API: Iterative CAD Refinement (compare-cad → regenerate) ─────────────────

@app.route('/api/refine-cad', methods=['POST'])
@login_required
@rate_limited
def api_refine_cad():
    """
    Accept compare-cad feedback (recommendations + lost/changed elements) and
    a prior source image, then submit a new Meshy generation with a
    feedback-enriched object_prompt.

    Body (multipart/form-data):
        image_0          – source image file (primary)
        image_1 … image_5 – optional additional angle files
        prompt           – original user prompt
        geometry_hint    – last extracted geometry hint
        compare_feedback – JSON string: { recommendations, design_lost, design_changed,
                                          similarity_score, verdict }
        category, target_use
        iteration        – integer (1-based, default 1)

    Returns: { "task_id": "…", "success": true, "refined_prompt": "…", "iteration": N }
    """
    meshy_key, err = get_meshy_key()
    if err:
        return jsonify({'error': err}), 500

    prompt        = request.form.get('prompt', '').strip()
    geometry_hint = request.form.get('geometry_hint', '').strip()
    target_use    = request.form.get('target_use', 'visualization').strip()
    iteration     = int(request.form.get('iteration', 1))

    compare_raw  = request.form.get('compare_feedback', '{}')
    try:
        feedback = json.loads(compare_raw)
    except Exception:
        feedback = {}

    recommendations = feedback.get('recommendations', [])
    design_lost     = feedback.get('design_lost', [])
    design_changed  = feedback.get('design_changed', [])
    score           = feedback.get('similarity_score', 0)
    verdict         = feedback.get('verdict', '')

    images_raw = []
    for i in range(6):
        f = request.files.get(f'image_{i}')
        if f and f.filename:
            try:
                images_raw.append(f.read())
            except Exception:
                pass

    if not images_raw:
        return jsonify({'error': 'No source image provided'}), 400

    # Build feedback-enriched prompt
    parts = []
    if geometry_hint: parts.append(geometry_hint)
    if prompt:        parts.append(prompt)
    if recommendations:
        parts.append(f'REFINEMENT INSTRUCTIONS (iteration {iteration}):')
        for rec in recommendations[:5]:
            parts.append(f'- {rec}')
    if design_lost:
        parts.append(f'MUST RESTORE missing elements: {", ".join(design_lost[:4])}.')
    if design_changed:
        parts.append(f'CORRECT distorted elements: {", ".join(design_changed[:3])}.')
    if score and verdict:
        parts.append(f'Previous fidelity score was {score}% ({verdict}). Improve it.')

    refined_prompt = ' '.join(parts).strip()[:500]

    try:
        pil_primary = Image.open(io.BytesIO(images_raw[0])).convert('RGB')
        w, h = pil_primary.size
        if w > 1200 or h > 1200:
            ratio = min(1200 / w, 1200 / h)
            pil_primary = pil_primary.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        pil_primary.save(buf, format='JPEG', quality=88)
        primary_bytes = buf.getvalue()
    except Exception as e:
        return jsonify({'error': f'Image decode failed: {e}'}), 400

    tmp_fn = f'refine_{uuid.uuid4().hex[:8]}.jpg'
    public_img_url = _upload_to_r2(primary_bytes, f'cad-inputs/{tmp_fn}')
    if not public_img_url:
        host = request.host_url.rstrip('/')
        if 'localhost' not in host and '127.0.0.1' not in host:
            tmp_path = os.path.join(app.config['UPLOAD_FOLDER'], tmp_fn)
            with open(tmp_path, 'wb') as fh:
                fh.write(primary_bytes)
            public_img_url = f'{host}/static/uploads/{tmp_fn}'

    payload = {
        'image_url':     public_img_url or f'data:image/jpeg;base64,{base64.b64encode(primary_bytes).decode()}',
        'enable_pbr':    True,
        'should_remesh': True,
        'object_prompt': refined_prompt,
    }

    if len(images_raw) > 1:
        ref_urls = []
        for idx, raw in enumerate(images_raw[1:6], start=1):
            try:
                pil = Image.open(io.BytesIO(raw)).convert('RGB').resize((1024, 1024), Image.LANCZOS)
                buf = io.BytesIO()
                pil.save(buf, format='JPEG', quality=85)
                jpg    = buf.getvalue()
                fn     = f'refine_ref_{uuid.uuid4().hex[:6]}_{idx}.jpg'
                r2_url = _upload_to_r2(jpg, f'cad-inputs/{fn}')
                ref_urls.append(r2_url or f'data:image/jpeg;base64,{base64.b64encode(jpg).decode()}')
            except Exception:
                pass
        if ref_urls:
            payload['reference_image_urls'] = ref_urls

    headers = {'Authorization': f'Bearer {meshy_key}', 'Content-Type': 'application/json'}
    log.info(f'[refine-cad] submitting | iter={iteration} | score_was={score} | prompt={refined_prompt[:80]}')

    try:
        res = requests.post(
            'https://api.meshy.ai/openapi/v1/image-to-3d',
            headers=headers, json=payload, timeout=30,
        )
        if res.status_code == 202:
            task_id = res.json().get('result')
            log.info(f'[refine-cad] task submitted | task_id={task_id}')
            return jsonify({'task_id': task_id, 'success': True,
                            'refined_prompt': refined_prompt, 'iteration': iteration})
        else:
            log.error(f'[refine-cad] Meshy error {res.status_code}: {res.text}')
            return jsonify({'error': f'Meshy API error: {res.status_code}', 'details': res.text[:300]}), 500
    except Exception as e:
        log.error(f'[refine-cad] exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


def _normalize_price_to_inr(price_str):
    if not price_str: return None
    price_str = price_str.strip()
    if 'Rs' in price_str or '₹' in price_str or 'INR' in price_str:
        return price_str
    m = re.search(r'\$\s*([\d,]+\.?\d*)', price_str)
    if m: return f'₹{int(float(m.group(1).replace(",",""))*83):,} (~${m.group(1)})'
    m = re.search(r'£\s*([\d,]+\.?\d*)', price_str)
    if m: return f'₹{int(float(m.group(1).replace(",",""))*105):,} (~£{m.group(1)})'
    m = re.search(r'€\s*([\d,]+\.?\d*)', price_str)
    if m: return f'₹{int(float(m.group(1).replace(",",""))*90):,} (~€{m.group(1)})'
    return price_str

def _fetch_og_thumbnail(url):
    if not url: return None
    try:
        resp = requests.get(url, timeout=(1.0, 1.5),
            headers={'User-Agent': 'Mozilla/5.0 (compatible; AtelierBot/1.0)'},
            allow_redirects=True, stream=True)
        if resp.status_code != 200: resp.close(); return None
        chunk = next(resp.iter_content(8192), b'')
        resp.close()
        text = chunk.decode('utf-8', errors='ignore')
        og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\'>]+)', text, re.IGNORECASE)
        if not og:
            og = re.search(r'<meta[^>]+content=["\']([^"\'>]+)["\'][^>]+property=["\']og:image', text, re.IGNORECASE)
        if og:
            img_url = og.group(1).strip()
            if img_url.startswith('//'): img_url = 'https:' + img_url
            if img_url.startswith('http'): return img_url
    except Exception: pass
    return None

@app.route('/api/market-research', methods=['POST'])
@login_required
@rate_limited
def api_market_research():
    category = request.form.get('category', 'Jewellery')
    keyword  = request.form.get('keyword', '').strip() or None
    image    = request.files.get('image')

    if not image:
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    client, err = get_gemini_client()
    if err:
        return jsonify({'success': False, 'error': err}), 500

    try:
        img_bytes = image.read()

        # Redis cache key: SHA1 of image bytes + category + keyword
        cache_key = 'glymr:market:' + hashlib.sha1(
            img_bytes + category.encode() + (keyword or '').encode()
        ).hexdigest()
        cached = cache_get(cache_key)
        if cached:
            log.info(f'[market-research] cache HIT | key={cache_key[:20]}…')
            return jsonify(cached)

        filter_note = f'Focus results specifically on listings matching: "{keyword}".' if keyword else ''

        prompt = f"""You are a jewellery market research expert for Indian sellers.
Analyse this {category} image — note style, material, gemstones, finish, design motifs.
Use Google Search to find at least 10 similar items currently being sold online.
{filter_note}

Respond ONLY with this exact JSON — no markdown, no code fences:
{{"keywords":["kw1","kw2"],"summary":"3-4 sentence market analysis.","listings":[{{"title":"Exact title","url":"https://full-url.com","source":"domain.com","price":"₹1,200","thumbnail":"https://cdn.url/img.jpg"}}],"price_range":{{"min":"₹500","max":"₹5,000"}}}}

RULES:
1. keywords: 8-10 SHORT tags (2-3 words max each)
2. listings: AT LEAST 10 real listings from Amazon.in, Flipkart, Myntra, Meesho, Nykaa, BlueStone, CaratLane, Tanishq, Craftsvilla, Etsy India
3. price: ALWAYS in ₹ (convert: 1 USD=₹83, 1 GBP=₹105)
4. thumbnail: direct CDN image URL ending in .jpg/.png/.webp, or null
5. summary: total sellers found, price spread, dominant platforms, competitive insight"""

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'), prompt],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
            http_options=types.HttpOptions(timeout=180_000),  # 180 s — Google Search grounding is slow
        )

        raw = response.text.strip().replace('```json', '').replace('```', '').strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return jsonify({'success': False, 'error': 'No JSON returned. Raw: ' + raw[:200]}), 500

        parsed = json.loads(match.group(0))
        listings = parsed.get('listings', [])

        enriched = [None] * len(listings)
        with ThreadPoolExecutor(max_workers=10) as pool:
            def enrich(item):
                p = _normalize_price_to_inr(item.get('price'))
                thumb = item.get('thumbnail') or (item.get('url') and _fetch_og_thumbnail(item['url']))
                return {'title': item.get('title',''), 'url': item.get('url',''), 'source': item.get('source',''), 'price': p, 'thumbnail': thumb}
            futures = {pool.submit(enrich, item): i for i, item in enumerate(listings)}
            try:
                for future in as_completed(futures, timeout=8):
                    enriched[futures[future]] = future.result(timeout=3)
            except Exception:
                for future, idx in futures.items():
                    if enriched[idx] is None:
                        enriched[idx] = {'title': listings[idx].get('title',''), 'url': listings[idx].get('url',''), 'source': listings[idx].get('source',''), 'price': _normalize_price_to_inr(listings[idx].get('price')), 'thumbnail': None}

        enriched = [e for e in enriched if e]
        result = {
            'success': True,
            'keywords': parsed.get('keywords', []),
            'summary': parsed.get('summary', ''),
            'listings': enriched,
            'seller_count': len(enriched),
            'price_range': parsed.get('price_range'),
        }
        cache_set(cache_key, result, ttl=3600)  # cache for 1 hour
        return jsonify(result)

    except Exception as e:
        log.error(f"[market-research] exception: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Google Trends ─────────────────────────────────────────────────────────

# Chart colours matched to glymr design tokens (cycled for up to 5 queries)
_TREND_COLORS = ['#b8924a', '#7a9e8e', '#c9786a', '#6b8cba', '#a37abf']

def _build_pytrends() -> '_TrendReq | None':
    """Return a TrendReq instance pre-loaded with the NID cookie (if set in env),
    or None if pytrends is unavailable."""
    if not _PYTRENDS_OK:
        return None
    nid = os.getenv('GOOGLE_NID_COOKIE', '')
    if nid:
        pt = _TrendReq(hl='en-IN', tz=330, timeout=(10, 30))
        # Inject the pre-fetched NID cookie to bypass Google's server-IP 403
        pt.cookies = {'NID': nid}
    else:
        pt = _TrendReq(hl='en-IN', tz=330, timeout=(10, 30))
    return pt


def _pytrends_interest(keywords: list[str], timeframe: str, geo: str) -> dict:
    """Fetch interest-over-time from Google Trends via pytrends.
    Returns {keyword: {labels, values, peak, avg, trend_delta}} or raises."""
    pt = _build_pytrends()
    if pt is None:
        raise RuntimeError('pytrends not installed')

    # Google Trends only accepts up to 5 keywords at once
    kw_list = keywords[:5]
    pt.build_payload(kw_list, cat=0, timeframe=timeframe, geo=geo, gprop='')
    iot = pt.interest_over_time()

    if iot.empty:
        raise ValueError('Google Trends returned empty data')

    result = {}
    for kw in kw_list:
        if kw not in iot.columns:
            continue
        series = iot[kw]
        vals   = [int(v) for v in series.tolist()]
        labels = [d.strftime('%b %d') for d in series.index]
        avg    = round(sum(vals) / len(vals), 1) if vals else 0
        peak   = max(vals) if vals else 0
        # Trend delta: last-4-weeks avg minus first-4-weeks avg
        delta  = round(
            (sum(vals[-4:]) / 4) - (sum(vals[:4]) / 4), 1
        ) if len(vals) >= 8 else 0
        # Related queries (best-effort)
        related = []
        try:
            rq  = pt.related_queries()
            top = rq.get(kw, {}).get('top')
            if top is not None and not top.empty:
                related = top['query'].head(5).tolist()
        except Exception:
            pass

        result[kw] = {
            'labels':  labels,
            'values':  vals,
            'peak':    peak,
            'avg':     avg,
            'delta':   delta,
            'related': related,
        }
    return result


@app.route('/api/trends', methods=['POST'])
@login_required
@rate_limited
def api_trends():
    """
    Fetch real Google Trends interest-over-time for up to 5 jewellery search queries.

    Body JSON:
        keywords  – list[str]  up to 5 search terms
        timeframe – str        e.g. "today 12-m" (default) | "today 3-m" | "today 5-y"
        geo       – str        ISO country code, default "IN"
    """
    body      = request.json or {}
    keywords  = [k.strip() for k in (body.get('keywords') or []) if k.strip()][:5]
    timeframe = body.get('timeframe', 'today 12-m').strip()
    geo       = body.get('geo', 'IN').strip().upper()

    if not keywords:
        return jsonify({'success': False, 'error': 'Provide at least one keyword.'}), 400

    # Cache key
    cache_key = 'glymr:trends:' + hashlib.sha1(
        json.dumps(sorted(keywords)).encode() + timeframe.encode() + geo.encode()
    ).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        log.info(f'[trends] cache HIT | {keywords}')
        return jsonify(cached)

    # ── Attempt 1: pytrends (real Google Trends data) ──────────────────────
    trends_data = None
    source      = 'google_trends'
    error_msg   = None

    try:
        trends_data = _pytrends_interest(keywords, timeframe, geo)
        log.info(f'[trends] pytrends OK | keywords={keywords}')
    except Exception as e:
        error_msg = str(e)
        log.warning(f'[trends] pytrends failed ({e}) — falling back to Gemini')

    # ── Attempt 2: Gemini + Google Search grounding ────────────────────────
    if not trends_data:
        source = 'gemini_search'
        try:
            client, err = get_gemini_client()
            if err:
                return jsonify({'success': False, 'error': f'Trends unavailable: {error_msg or err}'}), 500

            # Build 52-week label list for today (Gemini will fill in values)
            from datetime import date, timedelta
            today  = date.today()
            weeks  = [(today - timedelta(weeks=51-i)).strftime('%b %d') for i in range(52)]

            kw_json = json.dumps(keywords)
            prompt  = f"""You are a Google Trends data analyst for Indian jewellery searches.
For each keyword below, estimate the weekly Google Search interest in India over the last 12 months
(52 weeks, ending today {today.isoformat()}).
Use a 0-100 scale where 100 = peak popularity.
Base your estimates on real seasonal patterns, festival cycles (Diwali, Akshaya Tritiya, Navratri,
wedding season Oct-Feb), and known search trends for Indian jewellery.

Keywords: {kw_json}
Week labels (oldest → newest): {json.dumps(weeks)}

Respond ONLY with this exact JSON — no markdown, no extra keys:
{{"results": {{
  "<keyword>": {{
    "values": [<52 integers 0-100>],
    "related": ["top related search 1", "top related search 2", "top related search 3"]
  }}
}}}}"""

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt],
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            raw   = response.text.strip().replace('```json','').replace('```','').strip()
            match = re.search(r'\{[\s\S]*\}', raw)
            if not match:
                raise ValueError('No JSON in Gemini response')

            parsed  = json.loads(match.group(0))
            gem_res = parsed.get('results', {})

            trends_data = {}
            for kw in keywords:
                row = gem_res.get(kw) or gem_res.get(kw.lower(), {})
                vals = row.get('values', [50] * 52)
                # Pad / trim to exactly 52
                vals = (vals + [50] * 52)[:52]
                avg   = round(sum(vals) / len(vals), 1)
                peak  = max(vals)
                delta = round((sum(vals[-4:]) / 4) - (sum(vals[:4]) / 4), 1) if len(vals) >= 8 else 0
                trends_data[kw] = {
                    'labels':  weeks,
                    'values':  vals,
                    'peak':    peak,
                    'avg':     avg,
                    'delta':   delta,
                    'related': row.get('related', []),
                }
            log.info(f'[trends] Gemini fallback OK | keywords={keywords}')

        except Exception as e2:
            log.error(f'[trends] Gemini fallback failed: {e2}', exc_info=True)
            return jsonify({'success': False, 'error': f'Trends unavailable. pytrends: {error_msg}. Gemini: {e2}'}), 500

    # ── Attach chart colours ───────────────────────────────────────────────
    result_list = []
    for i, kw in enumerate(keywords):
        if kw not in trends_data:
            continue
        entry = {'keyword': kw, 'color': _TREND_COLORS[i % len(_TREND_COLORS)], **trends_data[kw]}
        result_list.append(entry)

    result = {
        'success':   True,
        'source':    source,
        'timeframe': timeframe,
        'geo':       geo,
        'series':    result_list,
    }
    cache_set(cache_key, result, ttl=3600)
    return jsonify(result)


# ── API: Gallery (DB-backed, user-scoped) ─────────────────────────────────────

@app.route('/api/gallery', methods=['GET'])
@login_required
def api_gallery_list():
    """Return gallery items for the current user, newest first."""
    if not _DB_URL:
        return jsonify({'items': [], 'warning': 'DATABASE_URL not set'}), 200
    user_id = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT id, url, type, category, title, tags, thumbnail_url, model_urls, created_at FROM gallery WHERE user_id=%s ORDER BY created_at DESC',
                    (user_id,)
                )
                rows = cur.fetchall()
        items = [
            {
                'id':            str(r['id']),
                'url':           r['url'],
                'type':          r['type'],
                'category':      r['category'],
                'title':         r['title'],
                'tags':          r['tags'],
                'thumbnail_url': r.get('thumbnail_url', ''),
                'model_urls':    r.get('model_urls', {}),
                'created_at':    r['created_at'].isoformat(),
            }
            for r in rows
        ]
        return jsonify({'items': items})
    except Exception as e:
        log.error(f'[gallery] list failed: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/gallery', methods=['POST'])
@login_required
def api_gallery_add():
    """Save a generated image to the gallery, linked to current user."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    user_id       = session['user_id']
    data          = request.json or {}
    url           = data.get('url', '').strip()
    img_type      = data.get('type', 'image')
    category      = data.get('category', '')
    title         = data.get('title', '')
    tags          = data.get('tags', [])
    thumbnail_url = data.get('thumbnail_url', '')   # fix #6: persist thumbnail
    if not url:
        return jsonify({'error': 'url required'}), 400
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gallery (user_id, url, type, category, title, tags, thumbnail_url)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (user_id, url, img_type, category, title, json.dumps(tags), thumbnail_url)
                )
                item_id = cur.fetchone()['id']
        return jsonify({'success': True, 'id': str(item_id)})
    except Exception as e:
        log.error(f'[gallery] add failed: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/gallery/<item_id>', methods=['DELETE'])
@login_required
def api_gallery_delete(item_id):
    """Delete a gallery item by UUID — only if it belongs to current user."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    user_id = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM gallery WHERE id=%s AND user_id=%s', (item_id, user_id))
        return jsonify({'success': True})
    except Exception as e:
        log.error(f'[gallery] delete failed: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/gallery', methods=['DELETE'])
@login_required
def api_gallery_clear():
    """Delete all gallery items for current user only."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    user_id = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM gallery WHERE user_id=%s', (user_id,))
        return jsonify({'success': True})
    except Exception as e:
        log.error(f'[gallery] clear failed: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/gallery/save-model', methods=['POST'])
@login_required
def api_gallery_save_model():
    """Save a completed 3D CAD model (from Meshy) to the gallery.

    Expected JSON body:
        {
            "glb_url":       "https://…/model.glb",   ← primary URL shown in gallery
            "thumbnail_url": "https://…/thumb.png",   ← preview image
            "model_urls":    {"glb": "…", "obj": "…", "fbx": "…", "usdz": "…"},
            "category":      "Necklace",
            "title":         "Gold Kundan Necklace"   ← optional
        }
    """
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500

    user_id = session['user_id']
    body    = request.json or {}

    glb_url       = body.get('glb_url', '').strip()
    thumbnail_url = body.get('thumbnail_url', '').strip()
    model_urls    = body.get('model_urls', {})
    category      = body.get('category', '')
    title         = body.get('title', '')

    if not glb_url:
        return jsonify({'error': 'glb_url required'}), 400

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gallery
                           (user_id, url, type, category, title, tags, thumbnail_url, model_urls)
                       VALUES (%s, %s, 'model', %s, %s, '[]'::jsonb, %s, %s)
                       RETURNING id""",
                    (user_id, glb_url, category, title, thumbnail_url, json.dumps(model_urls))
                )
                item_id = cur.fetchone()['id']
        log.info(f'[gallery] model saved | id={item_id} | user={user_id}')
        return jsonify({'success': True, 'id': str(item_id)})
    except Exception as e:
        log.error(f'[gallery] save-model failed: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── GLB proxy (avoids Meshy CDN CORS block in browser) ───────────────────────

@app.route('/api/proxy-glb')
@login_required
def api_proxy_glb():
    """Stream a GLB file from an external URL (e.g. Meshy CDN) through our
    server so the browser doesn't hit a CORS wall when Three.js loads it."""
    url = request.args.get('url', '').strip()
    if not url or not url.startswith('https://'):
        return jsonify({'error': 'Invalid URL'}), 400
    # Only proxy from known Meshy domains
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ''
    if not (host.endswith('meshy.ai') or host.endswith('meshy.oss-us-west-1.aliyuncs.com')):
        return jsonify({'error': 'URL not from an allowed domain'}), 403
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        from flask import Response, stream_with_context
        return Response(
            stream_with_context(r.iter_content(chunk_size=65536)),
            content_type=r.headers.get('Content-Type', 'model/gltf-binary'),
            headers={'Cache-Control': 'private, max-age=3600'},
        )
    except Exception as e:
        log.error(f'[proxy-glb] failed: {e}')
        return jsonify({'error': str(e)}), 502

# ── Projects API ───────────────────────────────────────────────────────────────

@app.route('/api/projects', methods=['GET'])
@login_required
def api_projects_list():
    """List all projects for current user."""
    if not _DB_URL:
        return jsonify({'projects': []}), 200
    uid = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT p.id, p.name, p.description, p.category, p.created_at, p.updated_at,
                              COUNT(g.id) AS item_count
                       FROM projects p
                       LEFT JOIN gallery g ON g.project_id = p.id
                       WHERE p.user_id = %s
                       GROUP BY p.id
                       ORDER BY p.updated_at DESC""",
                    (uid,)
                )
                rows = cur.fetchall()
        return jsonify({'projects': [
            {
                'id':          str(r['id']),
                'name':        r['name'],
                'description': r['description'],
                'category':    r['category'],
                'item_count':  r['item_count'],
                'created_at':  r['created_at'].isoformat(),
                'updated_at':  r['updated_at'].isoformat(),
            } for r in rows
        ]})
    except Exception as e:
        log.error(f'[projects] list error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects', methods=['POST'])
@login_required
def api_projects_create():
    """Create a new project."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    uid  = session['user_id']
    body = request.json or {}
    name = body.get('name', '').strip() or 'Untitled Project'
    desc = body.get('description', '').strip()
    cat  = body.get('category', '').strip()
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO projects (user_id, name, description, category)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (uid, name, desc, cat)
                )
                pid = str(cur.fetchone()['id'])
        cache_del(f'glymr:projects:{uid}')
        return jsonify({'success': True, 'id': pid, 'name': name})
    except Exception as e:
        log.error(f'[projects] create error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<project_id>', methods=['PATCH'])
@login_required
def api_projects_update(project_id):
    """Rename / re-describe a project."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    uid  = session['user_id']
    body = request.json or {}
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE projects SET name=%s, description=%s, category=%s, updated_at=NOW()
                       WHERE id=%s AND user_id=%s""",
                    (body.get('name','').strip() or 'Untitled Project',
                     body.get('description','').strip(),
                     body.get('category','').strip(),
                     project_id, uid)
                )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<project_id>', methods=['DELETE'])
@login_required
def api_projects_delete(project_id):
    """Delete a project (gallery items become orphaned, not deleted)."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    uid = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM projects WHERE id=%s AND user_id=%s', (project_id, uid))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/gallery/<item_id>/assign-project', methods=['POST'])
@login_required
def api_gallery_assign_project(item_id):
    """Move a gallery item into a project."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500
    uid  = session['user_id']
    body = request.json or {}
    pid  = body.get('project_id') or None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE gallery SET project_id=%s WHERE id=%s AND user_id=%s',
                    (pid, item_id, uid)
                )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── BOM / Costing Report ───────────────────────────────────────────────────────

# Metal density table (g/cm³)
_METAL_DENSITY = {
    '22K Yellow Gold': 17.7, 'Rose Gold': 14.6, 'White Gold / Silver': 14.6,
    'Oxidised Silver': 10.49, 'Platinum': 21.45, 'Panchdhatu': 10.5,
    '18K Yellow Gold': 15.5, '14K Yellow Gold': 13.1, 'Sterling Silver': 10.36,
}
# Rough INR price per gram (update periodically via env var)
_METAL_PRICE_PER_G_INR = {
    '22K Yellow Gold': float(os.getenv('GOLD_22K_PRICE_G', '6800')),
    'Rose Gold':       float(os.getenv('GOLD_ROSE_PRICE_G', '5500')),
    'White Gold / Silver': float(os.getenv('WHITE_GOLD_PRICE_G', '5200')),
    'Oxidised Silver': float(os.getenv('SILVER_PRICE_G', '90')),
    'Platinum':        float(os.getenv('PLATINUM_PRICE_G', '2800')),
    'Panchdhatu':      float(os.getenv('PANCHDHATU_PRICE_G', '220')),
    '18K Yellow Gold': float(os.getenv('GOLD_18K_PRICE_G', '5100')),
    '14K Yellow Gold': float(os.getenv('GOLD_14K_PRICE_G', '3900')),
    'Sterling Silver': float(os.getenv('STERLING_PRICE_G', '85')),
}
_STONE_PRICE_PER_CT_INR = {
    'round brilliant': 25000, 'oval': 20000, 'marquise': 18000,
    'princess cut': 22000, 'emerald cut': 19000, 'pear': 17000,
    'cushion': 21000, 'heart': 16000, 'ruby': 40000, 'emerald': 35000,
    'sapphire': 30000, 'no stones': 0,
}


@app.route('/api/generate-bom', methods=['POST'])
@login_required
@rate_limited
def api_generate_bom():
    """
    Generate a Bill of Materials + costing estimate for a jewellery design.

    Body (multipart or JSON):
        image_data  – base64 data-URI of the design image (for feature extraction)
        category    – jewellery category
        metal       – metal type
        gallery_id  – optional gallery item UUID to link the report to
        features    – optional JSON string of pre-extracted design features

    Returns rich JSON BOM with material weights, stone costs, labour, and total.
    """
    image_data  = request.form.get('image_data', '') or (request.json or {}).get('image_data', '')
    category    = (request.form.get('category') or (request.json or {}).get('category', 'Jewellery')).strip()
    metal       = (request.form.get('metal') or (request.json or {}).get('metal', '22K Yellow Gold')).strip()
    gallery_id  = request.form.get('gallery_id') or (request.json or {}).get('gallery_id')
    features_raw= request.form.get('features') or (request.json or {}).get('features')

    client, err = get_gemini_client()
    if err:
        return jsonify({'success': False, 'error': err}), 500

    # Use pre-extracted features if supplied, otherwise extract now
    features = {}
    if features_raw:
        try:
            features = json.loads(features_raw) if isinstance(features_raw, str) else features_raw
        except Exception:
            pass

    if not features and image_data:
        try:
            b64 = image_data.split(',', 1)[1] if ',' in image_data else image_data
            img_bytes = base64.b64decode(b64)
            pil = Image.open(io.BytesIO(img_bytes)).convert('RGB')
            feat_resp = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[f"""Extract jewellery design features for BOM. Return ONLY JSON:
{{"stone_count":null,"stone_type":"round brilliant","stone_size_hint":"","setting_type":"prong","prong_count":null,"shank_style":"plain band","category":"{category}","metal_finish":"{metal}","geometry_hint":""}}""", pil],
                config=types.GenerateContentConfig(temperature=0.1),
            )
            raw = feat_resp.text.strip().replace('```json','').replace('```','').strip()
            m   = re.search(r'\{[\s\S]*\}', raw)
            if m:
                features = json.loads(m.group(0))
        except Exception as e:
            log.warning(f'[bom] feature extraction failed: {e}')

    # ── Estimate metal volume from category ──────────────────────────────────
    # Rough volume estimates in cm³ for typical pieces (used when mesh unavailable)
    _CATEGORY_VOLUME_CM3 = {
        'Ring': 1.2, 'Necklace': 8.5, 'Earrings': 0.8, 'Bracelet': 5.5,
        'Anklet': 3.2, 'Brooch': 2.0, 'Jewellery Set': 12.0, 'Jewellery': 4.0,
    }
    vol_cm3   = _CATEGORY_VOLUME_CM3.get(category, 4.0)
    density   = _METAL_DENSITY.get(metal, 14.0)
    metal_g   = round(vol_cm3 * density, 2)
    metal_ppm = _METAL_PRICE_PER_G_INR.get(metal, 5000)
    metal_cost = round(metal_g * metal_ppm, 2)

    # ── Stone costs ──────────────────────────────────────────────────────────
    stone_count = features.get('stone_count') or 0
    stone_type  = (features.get('stone_type') or 'round brilliant').lower().strip()
    stone_ct    = 0.10  # default carat estimate per stone
    size_hint   = features.get('stone_size_hint', '')
    if 'large' in size_hint.lower():
        stone_ct = 0.50
    elif 'small' in size_hint.lower():
        stone_ct = 0.05
    stone_price_ct = _STONE_PRICE_PER_CT_INR.get(stone_type, 15000)
    stone_cost     = round(stone_count * stone_ct * stone_price_ct, 2)
    stone_details  = []
    if stone_count and stone_type != 'no stones':
        stone_details.append({
            'type':      stone_type,
            'count':     stone_count,
            'est_ct_ea': stone_ct,
            'price_ct':  stone_price_ct,
            'total_inr': stone_cost,
        })

    # ── Labour estimate ──────────────────────────────────────────────────────
    complexity  = 'medium'
    if features.get('setting_type') in ('pavé', 'invisible') or stone_count and stone_count > 10:
        complexity = 'high'
    elif not stone_count or stone_count < 3:
        complexity = 'low'
    labour_map = {'low': (2.0, 600), 'medium': (4.5, 800), 'high': (8.0, 1000)}
    labour_hrs, rate_per_hr = labour_map[complexity]
    labour_cost = round(labour_hrs * rate_per_hr, 2)

    # ── Making charges & markup ──────────────────────────────────────────────
    making_pct   = 0.15  # 15% on metal cost
    making_cost  = round(metal_cost * making_pct, 2)

    subtotal = metal_cost + stone_cost + labour_cost + making_cost
    gst      = round(subtotal * 0.03, 2)   # 3% GST on jewellery
    total    = round(subtotal + gst, 2)

    report = {
        'category':    category,
        'metal':       metal,
        'metal_weight_g':      metal_g,
        'metal_cost_inr':      metal_cost,
        'stone_details':       stone_details,
        'stone_cost_inr':      stone_cost,
        'labour_hrs':          labour_hrs,
        'labour_cost_inr':     labour_cost,
        'making_charges_inr':  making_cost,
        'subtotal_inr':        subtotal,
        'gst_inr':             gst,
        'total_inr':           total,
        'complexity':          complexity,
        'disclaimer': 'Estimates only. Actual costs depend on final design, current metal spot price, and craftsman rates.',
    }

    # Persist to DB
    uid = session.get('user_id')
    if _DB_URL and uid:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO bom_reports
                               (user_id, gallery_id, category, metal, metal_weight_g,
                                stone_details, labour_hrs, est_cost_inr, report_json)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (uid, gallery_id, category, metal, metal_g,
                         json.dumps(stone_details), labour_hrs, total, json.dumps(report))
                    )
        except Exception as e:
            log.warning(f'[bom] db save failed: {e}')

    track_usage('/api/generate-bom', 'gemini', tokens=400)
    return jsonify({'success': True, **report})


# ── Usage Dashboard API ────────────────────────────────────────────────────────

@app.route('/api/usage/summary', methods=['GET'])
@login_required
def api_usage_summary():
    """Return cost and call counts for the current user for the last 30 days."""
    if not _DB_URL:
        return jsonify({'summary': [], 'total_cost_usd': 0}), 200
    uid = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT endpoint, provider,
                              COUNT(*) AS calls,
                              SUM(tokens_used) AS tokens,
                              SUM(cost_usd) AS cost_usd
                       FROM api_usage
                       WHERE user_id = %s AND created_at > NOW() - INTERVAL '30 days'
                       GROUP BY endpoint, provider
                       ORDER BY cost_usd DESC""",
                    (uid,)
                )
                rows = cur.fetchall()
                cur.execute(
                    "SELECT SUM(cost_usd) AS total FROM api_usage WHERE user_id=%s AND created_at > NOW() - INTERVAL '30 days'",
                    (uid,)
                )
                total = cur.fetchone()['total'] or 0
        return jsonify({
            'summary': [dict(r) for r in rows],
            'total_cost_usd': float(total),
        })
    except Exception as e:
        log.error(f'[usage] summary error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Ring-size / Dimension Inference ───────────────────────────────────────────

# Standard ring size tables
_RING_SIZES = {
    'IN': {6: 15.7, 8: 16.5, 10: 17.4, 12: 18.2, 14: 18.9, 16: 19.8, 18: 20.6, 20: 21.3, 22: 22.2},
    'US': {'4': 14.8, '5': 15.7, '6': 16.5, '7': 17.4, '8': 18.2, '9': 18.9, '10': 19.8, '11': 20.6, '12': 21.3},
    'EU': {46: 14.6, 48: 15.3, 50: 15.9, 52: 16.6, 54: 17.2, 56: 17.8, 58: 18.5, 60: 19.1, 62: 19.7},
    'UK': {'H': 14.8, 'J': 15.3, 'L': 15.9, 'N': 16.8, 'P': 17.7, 'R': 18.5, 'T': 19.4, 'V': 20.2, 'X': 21.1},
}

# Standard jewellery dimensions (mm)
_CATEGORY_DIMS = {
    'Ring':          {'inner_diameter_mm': 17.4, 'band_width_mm': 3.0, 'height_mm': 8.0},
    'Necklace':      {'length_mm': 450, 'chain_width_mm': 2.0, 'pendant_height_mm': 30},
    'Earrings':      {'drop_mm': 35, 'width_mm': 15, 'post_diameter_mm': 0.9},
    'Bracelet':      {'inner_circumference_mm': 175, 'width_mm': 8.0, 'thickness_mm': 3.0},
    'Anklet':        {'inner_circumference_mm': 230, 'width_mm': 2.0},
    'Brooch':        {'width_mm': 40, 'height_mm': 30, 'pin_length_mm': 35},
    'Jewellery Set': {'necklace_length_mm': 450, 'earring_drop_mm': 30},
}


@app.route('/api/dimension-guide', methods=['GET'])
@login_required
def api_dimension_guide():
    """Return standard dimensions and ring size tables for a given category."""
    category = request.args.get('category', 'Ring').strip()
    ring_size = request.args.get('ring_size', '')
    region    = request.args.get('region', 'IN').upper()

    dims = _CATEGORY_DIMS.get(category, {})
    result = {'category': category, 'standard_dims_mm': dims}

    # Ring size lookup
    if category == 'Ring' and ring_size:
        size_table = _RING_SIZES.get(region, _RING_SIZES['IN'])
        try:
            key = int(ring_size) if ring_size.isdigit() else ring_size
            diameter_mm = size_table.get(key)
            if diameter_mm:
                result['ring_size']    = ring_size
                result['region']       = region
                result['diameter_mm']  = diameter_mm
                result['circumference_mm'] = round(diameter_mm * 3.14159, 1)
                result['scale_note']   = (
                    f'Scale your 3D model so the inner ring diameter equals {diameter_mm:.1f} mm '
                    f'({region} size {ring_size}).'
                )
        except (ValueError, TypeError):
            pass

    result['ring_size_table'] = _RING_SIZES.get(region, _RING_SIZES['IN'])
    result['all_categories']  = list(_CATEGORY_DIMS.keys())
    return jsonify(result)


@app.route('/api/scale-model', methods=['POST'])
@login_required
def api_scale_model():
    """
    Given a Meshy task ID (to get model statistics) and a target size, compute
    the scaling factor needed to achieve real-world dimensions.

    Body JSON:
        task_id        – Meshy task ID
        category       – jewellery category
        target_mm      – desired dimension in mm (e.g. inner diameter for ring)
        dimension_axis – 'x', 'y', or 'z' (default 'y')
        ring_size      – optional, e.g. '14' (IN) or '7' (US)
        region         – 'IN', 'US', 'EU', 'UK'
    """
    body      = request.json or {}
    task_id   = body.get('task_id', '').strip()
    category  = body.get('category', 'Ring').strip()
    target_mm = body.get('target_mm')
    ring_size = body.get('ring_size', '')
    region    = body.get('region', 'IN').upper()

    # Resolve target_mm from ring size if not given directly
    if not target_mm and ring_size and category == 'Ring':
        size_table = _RING_SIZES.get(region, _RING_SIZES['IN'])
        try:
            key = int(ring_size) if ring_size.isdigit() else ring_size
            target_mm = size_table.get(key)
        except (ValueError, TypeError):
            pass

    if not target_mm:
        target_mm = _CATEGORY_DIMS.get(category, {}).get('inner_diameter_mm', 17.4)

    # Fetch Meshy stats to get bounding box
    meshy_key, err = get_meshy_key()
    if err:
        return jsonify({'success': False, 'error': err}), 500

    model_size_mm = None
    if task_id:
        try:
            r = requests.get(
                f'https://api.meshy.ai/openapi/v1/image-to-3d/{task_id}',
                headers={'Authorization': f'Bearer {meshy_key}'},
                timeout=10,
            )
            if r.status_code == 200:
                stats = r.json().get('statistics', {})
                # Meshy uses metres internally; bounding_box in metres
                bbox = stats.get('bounding_box', {})
                axis_map = {'x': 'x', 'y': 'y', 'z': 'z'}
                axis = axis_map.get(body.get('dimension_axis', 'y').lower(), 'y')
                model_size_m  = bbox.get(f'size_{axis}', 0)
                model_size_mm = model_size_m * 1000 if model_size_m else None
        except Exception as e:
            log.warning(f'[scale-model] meshy fetch failed: {e}')

    # If no bounding box available, assume Meshy outputs at ~30mm scale
    assumed_size_mm = model_size_mm or 30.0
    scale_factor    = round(target_mm / assumed_size_mm, 4) if assumed_size_mm else 1.0

    # Additional standard-dimension context for the viewer to enforce
    std_dims  = _CATEGORY_DIMS.get(category, {})
    dim_label = 'inner_diameter' if category == 'Ring' else 'length' if category == 'Necklace' else 'dimension'

    return jsonify({
        'success':         True,
        'target_mm':       target_mm,
        'model_size_mm':   model_size_mm,
        'assumed_size_mm': assumed_size_mm,
        'scale_factor':    scale_factor,
        'scale_pct':       round(scale_factor * 100, 1),
        'dimension_label': dim_label,
        'standard_dims':   std_dims,
        'is_real_size':    model_size_mm is not None,   # True = Meshy bbox was available
        'note':            f'Apply ×{scale_factor} to the 3D model to achieve {target_mm}mm ({category}).',
        'warning':         None if model_size_mm else
                           f'Meshy bounding-box unavailable — scale computed from assumed {assumed_size_mm}mm baseline; verify in CAD software.',
    })


# ── Manufacturer Export API ────────────────────────────────────────────────────

@app.route('/api/export-manufacturer', methods=['POST'])
@login_required
def api_export_manufacturer():
    """
    Package a completed 3D model for manufacturer delivery.
    Creates a JSON spec sheet + download manifest that can be emailed to a manufacturer.

    Body JSON:
        task_id       – Meshy task ID
        category, metal, description, ring_size, region
        bom_data      – optional pre-generated BOM dict
    """
    body        = request.json or {}
    task_id     = body.get('task_id', '').strip()
    category    = body.get('category', 'Jewellery').strip()
    metal       = body.get('metal', '22K Yellow Gold').strip()
    description = body.get('description', '').strip()
    ring_size   = body.get('ring_size', '').strip()
    region      = body.get('region', 'IN').upper()
    bom_data    = body.get('bom_data', {})

    meshy_key, err = get_meshy_key()
    model_urls     = {}
    thumbnail_url  = ''
    stats          = {}
    if not err and task_id:
        try:
            r = requests.get(
                f'https://api.meshy.ai/openapi/v1/image-to-3d/{task_id}',
                headers={'Authorization': f'Bearer {meshy_key}'},
                timeout=10,
            )
            if r.status_code == 200:
                d             = r.json()
                model_urls    = d.get('model_urls', {})
                thumbnail_url = d.get('thumbnail_url', '')
                stats         = d.get('statistics', {})
        except Exception as e:
            log.warning(f'[export-mfr] meshy fetch failed: {e}')

    # Ring size dimension
    ring_dim = ''
    if category == 'Ring' and ring_size:
        size_table  = _RING_SIZES.get(region, _RING_SIZES['IN'])
        try:
            key     = int(ring_size) if ring_size.isdigit() else ring_size
            dia_mm  = size_table.get(key)
            ring_dim = f'{dia_mm}mm inner diameter (size {ring_size} {region})' if dia_mm else ''
        except Exception:
            pass

    spec = {
        'generated_at':   datetime.now(timezone.utc).isoformat(),
        'project_title':  description or f'{metal} {category}',
        'category':       category,
        'metal':          metal,
        'description':    description,
        'ring_size':      ring_size,
        'ring_dimension': ring_dim,
        'model_files':    model_urls,
        'thumbnail':      thumbnail_url,
        'mesh_statistics': stats,
        'bom':            bom_data,
        'instructions': [
            f'This is a {category} in {metal}.',
            f'Use the GLB/OBJ file for 3D printing or wax milling.',
            ring_dim and f'Scale model so inner bore = {ring_dim}.',
            'Finish: high polish unless specified otherwise.',
            'Contact designer for stone sourcing and setting instructions.',
        ],
        'contact': session.get('user_email', ''),
    }
    # Remove empty instructions
    spec['instructions'] = [i for i in spec['instructions'] if i]

    log.info(f'[export-mfr] spec generated | category={category} | metal={metal}')
    return jsonify({'success': True, 'spec': spec})


# ── Admin / Observability API ─────────────────────────────────────────────────

@app.route('/api/admin/stats', methods=['GET'])
@login_required
def api_admin_stats():
    """Return platform-wide usage stats. Only accessible to the first registered user (owner)."""
    if not _DB_URL:
        return jsonify({'error': 'DATABASE_URL not set'}), 500

    # Simple ownership check: first user by created_at is the admin
    uid = session['user_id']
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM users ORDER BY created_at ASC LIMIT 1')
                first = cur.fetchone()
        if not first or str(first['id']) != uid:
            return jsonify({'error': 'Forbidden'}), 403

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) AS n FROM users')
                user_count = cur.fetchone()['n']
                cur.execute("SELECT COUNT(*) AS n FROM gallery WHERE created_at > NOW() - INTERVAL '30 days'")
                gallery_30d = cur.fetchone()['n']
                cur.execute("""
                    SELECT endpoint, COUNT(*) AS calls, SUM(cost_usd) AS cost
                    FROM api_usage WHERE created_at > NOW() - INTERVAL '30 days'
                    GROUP BY endpoint ORDER BY calls DESC LIMIT 15
                """)
                usage_rows = cur.fetchall()
                cur.execute("SELECT SUM(cost_usd) AS total FROM api_usage WHERE created_at > NOW() - INTERVAL '30 days'")
                total_cost = cur.fetchone()['total'] or 0

        return jsonify({
            'user_count':    user_count,
            'gallery_30d':   gallery_30d,
            'total_cost_usd_30d': float(total_cost),
            'top_endpoints': [dict(r) for r in usage_rows],
        })
    except Exception as e:
        log.error(f'[admin-stats] error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Static file serving ────────────────────────────────────────────────────────

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/static/outputs/<filename>')
def output_file(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename)

# ── Main ───────────────────────────────────────────────────────────────────────

# Initialise DB tables (no-op if DATABASE_URL is not set)
with app.app_context():
    init_db()


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
    port = int(os.getenv('PORT', 5050))
    debug = not _IS_RAILWAY
    app.run(host='0.0.0.0', debug=debug, port=port)
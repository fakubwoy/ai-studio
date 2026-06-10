import os, json, base64, uuid, re, time, logging, hashlib, secrets, string, random
from concurrent.futures import ThreadPoolExecutor, as_completed
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
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
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

def init_db():
    """Create tables if they don't exist yet."""
    if not _DB_URL:
        log.warning('[db] DATABASE_URL not set — skipping DB init, falling back to JSON file')
        return
    try:
        conn = get_db()
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
        conn.commit()
        conn.close()
        log.info('[db] tables ready')
    except Exception as e:
        log.error(f'[db] init_db failed: {e}', exc_info=True)

# ── Redis ──────────────────────────────────────────────────────────────────────
_REDIS_URL = os.getenv('REDIS_URL', '')
_redis: redis_lib.Redis | None = None

def get_redis() -> redis_lib.Redis | None:
    """Return the Redis client, or None if unavailable."""
    global _redis
    if _redis is not None:
        return _redis
    if not _REDIS_URL:
        return None
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
_TMP = '/tmp' if _IS_RAILWAY else '.'

app.config['UPLOAD_FOLDER'] = os.path.join(_TMP, 'static', 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(_TMP, 'static', 'outputs')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
CATEGORIES_FILE = os.path.join(_TMP, 'categories.json')  # fallback only

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
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute('SELECT name, description, is_set, templates FROM categories ORDER BY name')
                rows = cur.fetchall()
            conn.close()
            if rows:
                cats = {
                    r['name']: {
                        'description': r['description'],
                        'is_set': r['is_set'],
                        'templates': r['templates'],
                    }
                    for r in rows
                }
                cache_set(_CATEGORIES_CACHE_KEY, cats, ttl=300)
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
            conn = get_db()
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
            conn.commit()
            conn.close()
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
    return genai.Client(api_key=api_key), None

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
        conn = get_db()
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
        conn.commit()
        conn.close()
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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM otp_codes
                   WHERE email=%s AND code=%s AND purpose=%s AND used=FALSE AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""",
                (email, code.strip(), purpose)
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return False
            cur.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (row['id'],))
        conn.commit()
        conn.close()
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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id, email, display_name, avatar_url, verified FROM users WHERE id=%s',
                (uid,)
            )
            row = cur.fetchone()
        conn.close()
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
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute('SELECT id, verified FROM users WHERE email=%s', (email,))
                row = cur.fetchone()
            conn.close()
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
        conn = get_db()
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

        conn.commit()
        conn.close()

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

        conn = get_db()
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
        conn.commit()
        conn.close()

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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id, email, display_name, avatar_url, password_hash, verified FROM users WHERE email=%s',
                (email,)
            )
            user = cur.fetchone()
        conn.close()
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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET last_login=NOW() WHERE id=%s', (user['id'],))
        conn.commit()
        conn.close()
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
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute('SELECT 1')
            conn.close()
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

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    user_id = session.get('user_id')
    concepts = []
    errors   = []

    for variation in variations:
        try:
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

            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
            )

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
            concepts.append(concept)

            # ── Auto-save to gallery ──────────────────────────────────────────
            if image_url and user_id and _DB_URL:
                try:
                    conn = get_db()
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO gallery (user_id, url, type, category, title, tags)
                               VALUES (%s, %s, 'concept', %s, %s, %s)""",
                            (user_id, image_url, category,
                             meta.get('title', ''), json.dumps(meta.get('tags', [])))
                        )
                    conn.commit()
                    conn.close()
                    log.info(f'[conceptualise] auto-saved to gallery: {image_url}')
                except Exception as db_err:
                    log.warning(f'[conceptualise] gallery auto-save failed: {db_err}')

        except Exception as e:
            log.error(f'[conceptualise] FAIL for variation={variation}: {e}', exc_info=True)
            errors.append(f'{variation}: {str(e)}')

    if not concepts:
        return jsonify({'error': 'All variations failed. ' + '; '.join(errors)}), 500

    return jsonify({'concepts': concepts, 'errors': errors if errors else None})

# ── API: Model Image Generation (existing logic, cleaned up) ───────────────────

@app.route('/api/generate-image', methods=['POST'])
@login_required
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
        key = f'cad-inputs/{filename}'
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
def api_generate_cad():
    """Submit an image to Meshy AI image-to-3D and return a task_id for polling.

    Accepts the image either as:
      • a multipart file field  (name='image')  — preferred, sent by cad.html
      • a base64 data-URI string (name='image_data') — legacy / fallback
    """
    prompt     = request.form.get('prompt', '').strip()
    art_style  = request.form.get('art_style', 'realistic')
    target_use = request.form.get('target_use', 'visualization')

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

        tmp_fn = f"cad_input_{uuid.uuid4()}.png"

        # ── Attempt 1: upload to R2 / S3 for a public URL ────────────────────
        public_img_url = _upload_to_r2(img_bytes, tmp_fn)

        # ── Attempt 2: use host URL (works when deployed publicly) ──────────
        if not public_img_url:
            tmp_path = os.path.join(app.config['UPLOAD_FOLDER'], tmp_fn)
            with open(tmp_path, 'wb') as f:
                f.write(img_bytes)
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
            # ── Attempt 3: send base64 directly — Meshy image-to-3D v1 supports it
            log.info("[cad] no public URL available, sending base64 data URI to Meshy")
            b64str = base64.b64encode(img_bytes).decode()
            payload = {
                "image_url": f"data:image/png;base64,{b64str}",
                "enable_pbr": True,
                "should_remesh": True,
            }

        if prompt:
            payload["object_prompt"] = prompt

        headers = {
            "Authorization": f"Bearer {meshy_key}",
            "Content-Type": "application/json",
        }

        log.info(f"[cad] submitting to Meshy AI (style={meshy_style})")
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


# ── API: Market Research ───────────────────────────────────────────────────────

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

# ── API: Gallery (DB-backed, user-scoped) ─────────────────────────────────────

@app.route('/api/gallery', methods=['GET'])
@login_required
def api_gallery_list():
    """Return gallery items for the current user, newest first."""
    if not _DB_URL:
        return jsonify({'items': [], 'warning': 'DATABASE_URL not set'}), 200
    user_id = session['user_id']
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id, url, type, category, title, tags, thumbnail_url, model_urls, created_at FROM gallery WHERE user_id=%s ORDER BY created_at DESC',
                (user_id,)
            )
            rows = cur.fetchall()
        conn.close()
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
    user_id  = session['user_id']
    data     = request.json or {}
    url      = data.get('url', '').strip()
    img_type = data.get('type', 'image')
    category = data.get('category', '')
    title    = data.get('title', '')
    tags     = data.get('tags', [])
    if not url:
        return jsonify({'error': 'url required'}), 400
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO gallery (user_id, url, type, category, title, tags)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (user_id, url, img_type, category, title, json.dumps(tags))
            )
            item_id = cur.fetchone()['id']
        conn.commit()
        conn.close()
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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute('DELETE FROM gallery WHERE id=%s AND user_id=%s', (item_id, user_id))
        conn.commit()
        conn.close()
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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute('DELETE FROM gallery WHERE user_id=%s', (user_id,))
        conn.commit()
        conn.close()
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
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO gallery
                       (user_id, url, type, category, title, tags, thumbnail_url, model_urls)
                   VALUES (%s, %s, 'model', %s, %s, '[]'::jsonb, %s, %s)
                   RETURNING id""",
                (user_id, glb_url, category, title, thumbnail_url, json.dumps(model_urls))
            )
            item_id = cur.fetchone()['id']
        conn.commit()
        conn.close()
        log.info(f'[gallery] model saved | id={item_id} | user={user_id}')
        return jsonify({'success': True, 'id': str(item_id)})
    except Exception as e:
        log.error(f'[gallery] save-model failed: {e}', exc_info=True)
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
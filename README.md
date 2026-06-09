# ◈ Atelié — AI-Powered Jewellery Studio

A full-stack Flask web application for jewellery designers to conceptualise, visualise, model in 3D, and research the market — all AI-powered.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env
# Edit .env and add your API keys, DATABASE_URL, and REDIS_URL

# 3. Start Postgres & Redis locally (Docker is easiest)
docker run -d --name glymr-pg  -e POSTGRES_PASSWORD=pass -e POSTGRES_DB=glymr -p 5432:5432 postgres:16
docker run -d --name glymr-redis -p 6379:6379 redis:7

# 4. Run
python app.py
```

Tables are created automatically on first run — no migration step needed.

Open [http://localhost:5050](http://localhost:5050)

---

## Modules

| Module | Path | AI Model | Key Required |
|---|---|---|---|
| Landing Page | `/` | — | — |
| Sketch Conceptualiser | `/studio/sketch` | Gemini 2.5 Flash Image | `GEMINI_API_KEY` |
| Model Try-On | `/studio/model` | Gemini 2.5 Flash Image | `GEMINI_API_KEY` |
| 3D CAD Generator | `/studio/cad` | Meshy AI Image-to-3D | `MESHY_API_KEY` |
| Market Trends | `/studio/market` | Gemini 2.5 Flash + Search | `GEMINI_API_KEY` |
| Gallery | `/studio/gallery` | — | — |

---

## API Keys & Services

### Gemini (required for Sketch, Model, Market)
1. Go to [https://aistudio.google.com](https://aistudio.google.com)
2. Create an API key
3. Add to `.env` as `GEMINI_API_KEY`

### Meshy AI (required for 3D CAD)
1. Sign up at [https://meshy.ai](https://meshy.ai)
2. Free tier available
3. Add to `.env` as `MESHY_API_KEY`
4. **Important**: Meshy needs to reach your server's public URL to fetch the input image. For local testing, use [ngrok](https://ngrok.com) or deploy to Railway.

### PostgreSQL (required for gallery persistence and categories)
- **Railway**: add the Postgres plugin — `DATABASE_URL` is injected automatically.
- **Local**: `postgresql://user:password@localhost:5432/glymr`
- Tables (`categories`, `gallery`) are created automatically on startup.
- Falls back to a local `categories.json` file if `DATABASE_URL` is not set.

### Redis (optional — enables caching)
- **Railway**: add the Redis plugin — `REDIS_URL` is injected automatically.
- **Local**: `redis://localhost:6379/0`
- If `REDIS_URL` is unset, the app runs without caching (slightly slower on repeated market research queries).
- **What's cached** (TTL):
  - `glymr:categories` — category list (5 min)
  - `glymr:market:<hash>` — market research results per image+keyword (1 hr)
  - `glymr:templates:<hash>` — AI-suggested templates per category (24 hr)

---

## Project Structure

```
atelié/
├── app.py                  # Flask backend — all routes & API logic
├── categories.json         # Jewellery categories & templates (auto-persisted)
├── requirements.txt
├── railway.toml            # Railway deployment config
├── Procfile
├── .env.example            # → copy to .env and fill keys
├── .gitignore
└── templates/
    ├── base.html           # Shared layout: nav, fonts, CSS variables, toast
    ├── landing.html        # Marketing landing page
    ├── sketch.html         # Sketch Conceptualiser studio
    ├── model.html          # Model Try-On studio
    ├── cad.html            # 3D CAD Generator
    ├── market.html         # Market Trends & Research
    └── gallery.html        # Saved generations gallery
```

No `static/css/` or `static/js/` directories are needed — all CSS and JS live inline in the Jinja templates, extending `base.html`.

---

## Cross-Module Navigation

Images flow between modules via `sessionStorage`:

| Source | Destination | Key |
|---|---|---|
| Sketch Studio (concept image) | Model Try-On | `pendingModelImage` |
| Sketch Studio (concept image) | 3D CAD | `pendingCADImage` |
| Model Try-On (output) | 3D CAD | `pendingCADImage` |
| Model Try-On (jewellery) | Market Trends | `pendingMarketImage` |
| Gallery | Model Try-On | `pendingModelImage` |

---

## Deployment (Railway)

1. Push to GitHub
2. Connect repo to [Railway](https://railway.app)
3. Add a **Postgres** plugin — Railway sets `DATABASE_URL` automatically
4. Add a **Redis** plugin — Railway sets `REDIS_URL` automatically
5. Add environment variables (`GEMINI_API_KEY`, `MESHY_API_KEY`)
6. Deploy — `railway.toml` handles the rest

---

## Notes

- Gallery items are stored in **PostgreSQL** (`gallery` table), shared across all sessions and deploys
- Categories are stored in **PostgreSQL** (`categories` table), with a JSON file fallback for local dev without Postgres
- Uploaded/output images go to `/tmp/static/` on Railway (ephemeral), or `./static/` locally — store images externally (S3/R2) for permanent storage
- The 3D CAD feature requires Meshy to fetch your image via a public URL — deploy to Railway or use ngrok for local testing
- Redis caching is optional but recommended to avoid redundant Gemini calls for market research
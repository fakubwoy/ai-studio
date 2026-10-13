# ◈ Atelié

A Flask web app that takes a jewellery design from sketch to concept render, on-model shot, 3D model and market research, using Gemini and Meshy.

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3-000000?logo=flask&logoColor=white)
![Gunicorn](https://img.shields.io/badge/Gunicorn-gevent-499848?logo=gunicorn&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Gemini](https://img.shields.io/badge/Google-Gemini%202.5-4285F4?logo=google&logoColor=white)
![Meshy](https://img.shields.io/badge/Meshy-Image--to--3D-6E40C9)
![Three.js](https://img.shields.io/badge/Three.js-r128-000000?logo=threedotjs&logoColor=white)
![Cloudflare R2](https://img.shields.io/badge/Cloudflare-R2-F38020?logo=cloudflare&logoColor=white)
![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?logo=railway&logoColor=white)

## Overview

A designer signs in (email OTP, email and password, or Google) and works through these studios:

| Studio | Path | What it does | Backed by |
|---|---|---|---|
| Sketch Conceptualiser | `/studio/sketch` | Turns a sketch and/or text brief into up to 4 concept variations, with title, description and tags. Optional sketch clean-up before generation. | `gemini-2.5-flash-image`, `gemini-2.5-flash` |
| Model Try-On | `/studio/model` | From a photo of a piece, generates a white-background product shot and an on-model shot in parallel, using per-category placement templates. Background removal and result analysis. | `gemini-2.5-flash-image`, `gemini-2.5-flash` |
| 3D CAD Generator | `/studio/cad` | Image-to-3D from one image, multiple angles or a turntable set. Feature extraction to guide the mesh, 2D vs 3D comparison, refinement, mesh analysis, scaling to real dimensions, STEP/IGES export and a manufacturer package. In-browser GLB viewer. | Meshy image-to-3D, `gemini-2.5-flash`, trimesh |
| Market Trends | `/studio/market` | Finds similar listings on Indian marketplaces with search-grounded Gemini, verifies every listing URL, and plots Google Trends interest over time. | `gemini-2.5-flash` + Google Search, pytrends |
| Gallery and Projects | `/studio/gallery`, `/studio/projects` | Per-user store of generated images and 3D models, grouped into projects. | PostgreSQL |

Images move between studios in the browser through `sessionStorage` keys (`pendingModelImage`, `pendingCADImage`, `pendingMarketImage` and related), so a concept can go straight to try-on, CAD or market research without a re-upload.

## Architecture

The whole backend is one module, `app.py`. Pages are server-rendered Jinja templates with inline CSS and JS; the rest of the app is a JSON API under `/api`.

```mermaid
flowchart LR
    B["Browser<br/>Jinja pages, Three.js, Chart.js"]

    subgraph APP["Flask app (app.py) on Gunicorn + gevent"]
        direction TB
        MW["login_required / rate_limited<br/>decorators"]
        R["Routes<br/>/auth, /studio/*, /api/*"]
        S["Helpers<br/>_generate_with_gemini, _upload_to_r2,<br/>job_set / job_get, cache_get / cache_set"]
        BG["Background thread<br/>_run_market_research_job"]
        MW --> R --> S
        R --> BG
    end

    B -->|"HTTP + session cookie"| MW

    S --> GEM["Google Gemini API<br/>2.5 Flash, 2.5 Flash Image, Search grounding"]
    S --> MESHY["Meshy API<br/>image-to-3D"]
    S --> R2["Cloudflare R2<br/>via boto3 (optional)"]
    S --> RESEND["Resend<br/>OTP email"]
    S --> GOAUTH["Google OAuth<br/>userinfo"]
    S --> TRENDS["Google Trends<br/>via pytrends"]
    BG --> GEM

    S --> PG[("PostgreSQL<br/>users, otp_codes, categories,<br/>gallery, projects, api_usage, bom_reports")]
    S --> RD[("Redis<br/>cache, rate limits, job state")]
    BG --> RD
    S --> FS[("Local disk / Railway volume<br/>uploads/, outputs/")]
```

### Data stores

| Store | Used for | If not configured |
|---|---|---|
| PostgreSQL | Users, OTP codes, categories and templates, gallery, projects, API usage, BOM reports. Tables are created on startup (`init_db`), no migration step. | Categories fall back to `categories.json`; gallery and projects return empty. |
| Redis | Category cache (5 min), market research results (1 hr), AI template suggestions (24 hr), per-user hourly rate limits, background job state (10 min). | Caching and rate limiting are skipped; job state is kept in process memory. |
| Cloudflare R2 | Public URLs for generated images and for CAD input images that Meshy needs to fetch. | Files are written to `uploads/` and `outputs/` under `/data` on Railway or `.` locally. |

### Market research flow

Market research runs in a background thread so a slow search-grounded Gemini call (10s to about 2 min) does not hold the request or hit the Gunicorn worker timeout. The browser polls for real progress checkpoints.

```mermaid
sequenceDiagram
    autonumber
    participant UI as market.html
    participant API as Flask /api/market-research
    participant RD as Redis
    participant JOB as _run_market_research_job
    participant GEM as Gemini 2.5 Flash + Google Search
    participant WEB as Listing URLs

    UI->>API: POST image, category, keyword
    API->>RD: GET glymr:market:<sha1(image+category+keyword)>
    alt cache hit
        API->>RD: SETEX glymr:job:<id> status=done, result
    else cache miss
        API->>RD: SETEX glymr:job:<id> status=running, stage=queued
        API-)JOB: start daemon thread
    end
    API-->>UI: job_id

    loop until status is done or error
        UI->>API: GET /api/market-research/status/<id>
        API->>RD: GET glymr:job:<id>
        API-->>UI: stage, message, pct
    end

    JOB->>GEM: generate_content(image, prompt, google_search tool)
    Note over JOB,GEM: 45s hard timeout per call, retries with backoff,<br/>falls back to gemini-2.5-flash-lite, 130s total budget
    GEM-->>JOB: JSON keywords, summary, listings
    JOB->>RD: stage=parsing
    par up to 10 workers
        JOB->>WEB: GET listing URL, read og:image
        WEB-->>JOB: reachable or not
    end
    Note over JOB: unreachable URLs are replaced with a<br/>Google Shopping search link scoped to the marketplace
    JOB->>RD: SETEX glymr:market:<hash> result (1 hr)
    JOB->>RD: SETEX glymr:job:<id> status=done, result
```

The 3D CAD flow uses the same submit-then-poll shape without a background thread: `/api/generate-cad` uploads the input to R2 (or serves it from the public host, or falls back to a base64 data URI), submits it to Meshy and returns a `task_id`; the page polls `/api/cad-status/<task_id>` until Meshy reports `SUCCEEDED`, then loads the GLB through `/api/proxy-glb` to avoid CORS.

## Features

- Concept generation from sketch and/or text, up to 4 variations per request, each auto-saved to the gallery.
- Product shot and on-model shot generated in parallel from the same reference.
- Editable jewellery categories and placement templates, with AI-suggested templates per category.
- 3D generation from single, multi-angle or turntable images; refinement driven by a 2D vs 3D comparison.
- Mesh analysis, scaling to a target size, STEP export (pythonOCC if installed, otherwise a trimesh shell, otherwise IGES) and a manufacturer export package.
- Bill of materials and cost estimate in INR, with per-gram metal prices configurable through env vars.
- Search-grounded market research with live URL verification, plus Google Trends charts (pytrends first, Gemini fallback).
- Per-user rate limits on the expensive endpoints (for example 10 CAD jobs and 20 concept runs per hour) and usage tracking per user, with an owner-only stats endpoint.
- `/healthz` reports database and Redis connectivity.

## Environment variables

There is no `.env.example` in the repo; this list is taken from the `os.getenv` calls in `app.py`.

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Gemini for concepts, try-on, analysis, market research, BOM |
| `MESHY_API_KEY` | For 3D CAD | Meshy image-to-3D |
| `DATABASE_URL` | Recommended | PostgreSQL connection string |
| `REDIS_URL` | Optional | Redis for caching, rate limits and job state |
| `SECRET_KEY` | Yes in production | Flask session signing; without it each worker generates its own key and sessions break |
| `RESEND_API_KEY` | For email OTP | Sends OTP codes through Resend |
| `RESEND_SENDER_EMAIL` | Optional | From address for OTP emails |
| `GOOGLE_CLIENT_ID` | For Google sign-in | Google Identity Services client on the auth page |
| `R2_ENDPOINT_URL` | Optional | Cloudflare R2 S3 endpoint |
| `R2_ACCESS_KEY_ID` | Optional | R2 credentials |
| `R2_SECRET_ACCESS_KEY` | Optional | R2 credentials |
| `R2_BUCKET_NAME` | Optional | R2 bucket |
| `R2_PUBLIC_URL` | Optional | Public base URL for R2 objects |
| `GOOGLE_NID_COOKIE` | Optional | Google NID cookie passed to pytrends to reduce Trends blocking |
| `MOBILE_API_KEY` | Optional | Shared key accepted in the `X-Mobile-Key` header instead of a session |
| `DEV_BYPASS` | Local only | `true` disables auth locally; ignored when `RAILWAY_ENVIRONMENT` is set |
| `PORT` | Optional | Port for `python app.py` (default 5050) |
| `GOLD_22K_PRICE_G`, `GOLD_18K_PRICE_G`, `GOLD_14K_PRICE_G`, `GOLD_ROSE_PRICE_G`, `WHITE_GOLD_PRICE_G`, `SILVER_PRICE_G`, `STERLING_PRICE_G`, `PLATINUM_PRICE_G`, `PANCHDHATU_PRICE_G` | Optional | Metal prices per gram (INR) used by the BOM estimate |

`RAILWAY_ENVIRONMENT` is set by Railway itself and switches storage to the `/data` volume and turns off debug mode.

## Repository structure

```
ai-studio/
├── app.py              # Flask app: config, DB schema, auth, all routes and AI integrations
├── categories.json     # Category and template fallback when DATABASE_URL is unset
├── requirements.txt
├── Procfile            # Gunicorn start command
├── railway.toml        # Railway build, start, healthcheck and volume config
├── templates/
│   ├── base.html       # Shared layout, nav, CSS variables, toast
│   ├── landing.html
│   ├── auth.html       # OTP, password and Google sign-in
│   ├── sketch.html     # Sketch Conceptualiser
│   ├── model.html      # Model Try-On
│   ├── cad.html        # 3D CAD Generator and GLB viewer
│   ├── market.html     # Market Trends
│   ├── gallery.html
│   └── projects.html
└── static/
    ├── images/         # Landing and gallery imagery
    └── videos/         # Landing page videos
```

## Running locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create .env with at least GEMINI_API_KEY, DATABASE_URL and REDIS_URL
#    (see the table above). Add DEV_BYPASS=true to skip sign-in locally.

# 3. Start Postgres and Redis
docker run -d --name glymr-pg -e POSTGRES_PASSWORD=pass -e POSTGRES_DB=glymr -p 5432:5432 postgres:16
docker run -d --name glymr-redis -p 6379:6379 redis:7
#    DATABASE_URL=postgresql://postgres:pass@localhost:5432/glymr
#    REDIS_URL=redis://localhost:6379/0

# 4. Run
python app.py
```

Open http://localhost:5050. Tables are created on first start.

Docker is only used here for Postgres and Redis; the repo has no Dockerfile for the app.

**3D CAD locally:** Meshy has to fetch the input image. Configure R2 so the image gets a public URL, or expose the app with a tunnel such as ngrok. Without either, the app sends the image to Meshy as a base64 data URI.

## Deployment (Railway)

`railway.toml` builds with Nixpacks and starts:

```
gunicorn app:app --bind 0.0.0.0:$PORT --worker-class gevent --workers 2 --worker-connections 100 --timeout 300 --graceful-timeout 30
```

with a healthcheck on `/`, restart on failure, and a volume `glymr-storage` mounted at `/data` for uploads and outputs. The `Procfile` carries the same command.

1. Connect the repo to Railway.
2. Add the Postgres and Redis plugins (they inject `DATABASE_URL` and `REDIS_URL`).
3. Set `SECRET_KEY`, `GEMINI_API_KEY`, `MESHY_API_KEY` and any of the optional variables above.
4. Deploy.

## Limitations

- All backend code lives in a single `app.py` (about 5,000 lines); there are no tests.
- Market research job state is held for 10 minutes; without Redis it lives in one worker's memory, so a poll that lands on the other worker returns "unknown job".
- Without R2, generated files live on the local disk or the Railway volume and are served by Flask.
- The admin stats endpoint treats the first registered user as the owner.
- STEP export from the trimesh path is a triangulated shell, not true BREP; install `pythonocc-core` for the full conversion.

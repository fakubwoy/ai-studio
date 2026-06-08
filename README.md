# ◈ Atelié — AI-Powered Jewellery Studio

A full-stack Flask web application for jewellery designers to conceptualise, visualise, model in 3D, and research the market — all AI-powered.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env
# Edit .env and add your API keys

# 3. Run
python app.py
```

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

## API Keys

### Gemini (required for Sketch, Model, Market)
1. Go to [https://aistudio.google.com](https://aistudio.google.com)
2. Create an API key
3. Add to `.env` as `GEMINI_API_KEY`

### Meshy AI (required for 3D CAD)
1. Sign up at [https://meshy.ai](https://meshy.ai)
2. Free tier available
3. Add to `.env` as `MESHY_API_KEY`
4. **Important**: Meshy needs to reach your server's public URL to fetch the input image. For local testing, use [ngrok](https://ngrok.com) or deploy to Railway.

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
3. Add environment variables (`GEMINI_API_KEY`, `MESHY_API_KEY`)
4. Deploy — `railway.toml` handles the rest

---

## Notes

- Gallery items are stored in `localStorage` (per-browser, session-persistent)
- Uploaded/output images go to `/tmp/static/` on Railway (ephemeral), or `./static/` locally
- The 3D CAD feature requires Meshy to fetch your image via a public URL — deploy to Railway or use ngrok for local testing
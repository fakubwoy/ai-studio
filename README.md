# ◈ Jewellery AI Studio

AI-powered pipeline to place jewellery images onto models using Google Gemini.

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set up your API key
```bash
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY
```

### 3. Run the app
```bash
python app.py
```

Open http://localhost:5050 in your browser.

---

## Features

- **Upload** any jewellery photo (PNG, JPG, WEBP)
- **Categories**: Necklace, Earrings, Ring, Bracelet, Anklet, Brooch — plus add your own
- **AI Template Suggestions**: Gemini analyzes the category and generates sizing/placement templates
- **Generate**: Places the jewellery on a model with correct sizing using Gemini Imagen
- **Gallery**: All generated images saved in session
- **Custom Templates**: Add your own sizing specs per category

## Image Generation Providers

| Provider | Cost | Quality | Setup |
|----------|------|---------|-------|
| Gemini Imagen | Free (uses your key) | Good | Just your GEMINI_API_KEY |
| Stability AI | ~$0.04/image | Excellent | Add STABILITY_API_KEY to .env |

## Notes

- Gemini image generation requires **Gemini 2.0 Flash Experimental** access
- If Gemini returns no image, try Stability AI for guaranteed output
- All uploads and outputs are stored in `static/uploads/` and `static/outputs/`
- Category templates are persisted in `categories.json`

## Folder Structure
```
jewellery-pipeline/
├── app.py              # Flask backend
├── categories.json     # Saved categories & templates (auto-created)
├── requirements.txt
├── .env                # Your API keys (create from .env.example)
├── templates/
│   └── index.html      # Main UI
└── static/
    ├── css/style.css
    ├── js/app.js
    ├── uploads/        # Uploaded jewellery images
    └── outputs/        # Generated model images
```

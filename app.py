import os, json, base64, uuid, re, time, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from PIL import Image
import io
from dotenv import load_dotenv
import requests
from flask_cors import CORS

load_dotenv()

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('jewellery_ai')

app = Flask(__name__)
CORS(app)  # Allow mobile app requests

# ── Per-request logging ────────────────────────────────────────────────────────
@app.before_request
def _log_request():
    from flask import g
    g.start_time = time.time()
    log.info(f"→ {request.method} {request.path} | ip={request.remote_addr} | size={request.content_length or 0}b")

@app.after_request
def _log_response(response):
    from flask import g
    duration = (time.time() - g.get('start_time', time.time())) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    log.log(level, f"← {request.method} {request.path} | status={response.status_code} | {duration:.0f}ms")
    return response

# Railway has an ephemeral filesystem — use /tmp for uploads/outputs
# and keep categories.json in /tmp too so writes don't fail
_IS_RAILWAY = os.getenv('RAILWAY_ENVIRONMENT') is not None
_TMP = '/tmp' if _IS_RAILWAY else '.'

app.config['UPLOAD_FOLDER'] = os.path.join(_TMP, 'static', 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(_TMP, 'static', 'outputs')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB for mobile uploads
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

CATEGORIES_FILE = os.path.join(_TMP, 'categories.json')

# Ensure directories exist at module load time (for gunicorn workers)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
DEFAULT_CATEGORIES = {
    "Necklace": {
        "description": "Neck jewellery worn around the neck",
        "templates": [
            {"name": "Choker", "placement": "sits tight around the neck base", "size_hint": "35-41cm, sits at throat level", "model_pose": "front-facing, chin slightly up"},
            {"name": "Princess", "placement": "rests on collarbone", "size_hint": "43-50cm, at collarbone", "model_pose": "front-facing, natural posture"},
            {"name": "Matinee", "placement": "falls between collarbone and bust", "size_hint": "50-60cm, mid-chest", "model_pose": "slight three-quarter turn"},
            {"name": "Opera", "placement": "long chain reaching sternum/below bust", "size_hint": "70-90cm, below bust", "model_pose": "slight lean, three-quarter view"},
            {"name": "Rope/Lariat", "placement": "very long, can be looped", "size_hint": "90cm+, can be doubled", "model_pose": "full body, artistic drape"}
        ]
    },
    "Earrings": {
        "description": "Ear jewellery",
        "templates": [
            {"name": "Stud", "placement": "sits flush on earlobe", "size_hint": "4-10mm diameter, at earlobe center", "model_pose": "three-quarter face turn, hair swept back"},
            {"name": "Hoop", "placement": "circular ring through earlobe", "size_hint": "20-50mm diameter, hanging from lobe", "model_pose": "profile or three-quarter, hair back"},
            {"name": "Drop/Dangle", "placement": "hangs below earlobe", "size_hint": "3-7cm drop from lobe", "model_pose": "three-quarter turn, head slightly tilted"},
            {"name": "Chandelier", "placement": "multi-tier dramatic drop", "size_hint": "5-10cm elaborate drop", "model_pose": "face forward, chin up, hair pinned up"},
            {"name": "Huggie", "placement": "hugs the earlobe closely", "size_hint": "10-15mm, close to lobe", "model_pose": "close profile shot"}
        ]
    },
    "Ring": {
        "description": "Finger jewellery",
        "templates": [
            {"name": "Solitaire", "placement": "single stone on band", "size_hint": "standard ring width 2-4mm, stone 5-8mm", "model_pose": "hand extended forward, fingers spread"},
            {"name": "Cocktail/Statement", "placement": "large decorative ring", "size_hint": "wide band or large stone 10-20mm", "model_pose": "hand raised, fingers elegantly spread"},
            {"name": "Band", "placement": "simple flat band", "size_hint": "2-8mm width, flat profile", "model_pose": "hand natural, slight angle"},
            {"name": "Eternity Band", "placement": "stones all around band", "size_hint": "2-4mm band with continuous stones", "model_pose": "hand angled showing full band"},
            {"name": "Stackable", "placement": "thin ring meant to stack", "size_hint": "1-2mm ultra thin band", "model_pose": "multiple fingers showing stack"}
        ]
    },
    "Bracelet": {
        "description": "Wrist jewellery",
        "templates": [
            {"name": "Tennis Bracelet", "placement": "delicate in-line stones around wrist", "size_hint": "17-19cm circumference, 3-5mm wide", "model_pose": "wrist extended, arm slightly bent"},
            {"name": "Bangle", "placement": "rigid circular bracelet", "size_hint": "60-65mm inner diameter, rigid", "model_pose": "arm raised, wrist turned outward"},
            {"name": "Cuff", "placement": "open-ended wide bracelet", "size_hint": "wide 2-5cm, open at back", "model_pose": "forearm forward, wrist turned"},
            {"name": "Charm Bracelet", "placement": "chain with hanging charms", "size_hint": "18-20cm chain, charms vary", "model_pose": "wrist at angle showing charms"},
            {"name": "Chain Bracelet", "placement": "delicate chain around wrist", "size_hint": "17-20cm chain, 1-3mm links", "model_pose": "wrist forward, fingers relaxed"}
        ]
    },
    "Anklet": {
        "description": "Ankle jewellery",
        "templates": [
            {"name": "Delicate Chain", "placement": "thin chain around ankle", "size_hint": "22-25cm, fine chain", "model_pose": "leg extended, bare ankle visible"},
            {"name": "Beaded", "placement": "beaded chain on ankle", "size_hint": "23-26cm, bead sizes vary", "model_pose": "seated or standing, bare ankle"},
            {"name": "Charm Anklet", "placement": "chain with small charms", "size_hint": "22-26cm with dangling charms", "model_pose": "walking pose or seated showing anklet"}
        ]
    },
    "Brooch": {
        "description": "Pin/brooch for garments",
        "templates": [
            {"name": "Lapel Pin", "placement": "pinned to jacket lapel", "size_hint": "2-3cm, left chest lapel", "model_pose": "three-quarter turn, jacket visible"},
            {"name": "Scarf Pin", "placement": "holds scarf in place", "size_hint": "3-6cm, at scarf knot", "model_pose": "facing forward, scarf draped"},
            {"name": "Statement Brooch", "placement": "large decorative chest piece", "size_hint": "5-10cm, upper chest area", "model_pose": "front facing, upper body shot"}
        ]
    }
}

def load_categories():
    if os.path.exists(CATEGORIES_FILE):
        with open(CATEGORIES_FILE, 'r') as f:
            return json.load(f)
    save_categories(DEFAULT_CATEGORIES)
    return DEFAULT_CATEGORIES

def save_categories(cats):
    with open(CATEGORIES_FILE, 'w') as f:
        json.dump(cats, f, indent=2)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_gemini_client():
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return None, "GEMINI_API_KEY not set in .env file"
    # market-research with Google Search grounding can take 60-120s
    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": 240},
    )
    return client, None

@app.route('/')
def index():
    categories = load_categories()
    return render_template('index.html', categories=list(categories.keys()))

@app.route('/api/categories', methods=['GET'])
def get_categories():
    return jsonify(load_categories())

@app.route('/api/categories/add', methods=['POST'])
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
def suggest_templates():
    data = request.json
    category = data.get('category', '')
    description = data.get('description', '')

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    prompt = f"""You are a professional jewellery sizing and styling expert.
For the jewellery category: "{category}" ({description})

{"IMPORTANT: This is a JEWELLERY SET category — templates must describe how MULTIPLE pieces are worn TOGETHER on a model simultaneously (e.g. necklace on neck + earrings on both ears at the same time). Never describe just one piece in isolation." if 'set' in category.lower() else ""}

Generate a comprehensive list of sizing templates that a fashion photographer or AI image generator would need to correctly place and size this jewellery on a model.

Return ONLY a JSON array (no markdown, no explanation) like this:
[
  {{
    "name": "Template name",
    "placement": "exactly where and how it sits on the body",
    "size_hint": "measurements, dimensions, proportions",
    "model_pose": "ideal model pose/angle to showcase this style"
  }}
]

Include 4-7 templates covering the common styles/variations of this jewellery type. Be specific with measurements and placement instructions."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        text = response.text.strip()
        # Strip markdown code blocks if present
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        templates = json.loads(text)

        cats = load_categories()
        if category in cats:
            # Merge with existing, avoid duplicates
            existing_names = {t['name'] for t in cats[category].get('templates', [])}
            new_templates = [t for t in templates if t['name'] not in existing_names]
            cats[category]['templates'].extend(new_templates)
            save_categories(cats)

        return jsonify({'templates': templates, 'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/add-template', methods=['POST'])
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

@app.route('/api/generate-image', methods=['POST'])
def generate_image():
    files = request.files.getlist('jewellery_image')
    if not files or not files[0].filename:
        return jsonify({'error': 'No image uploaded'}), 400

    category = request.form.get('category', '')
    template_json = request.form.get('template', '{}')
    custom_prompt = request.form.get('custom_prompt', '')
    negative_prompt = request.form.get('negative_prompt', '')
    model_preference = request.form.get('model_preference', 'diverse')
    duplication_guard = request.form.get('duplication_guard', 'false').lower() == 'true'

    try:
        template = json.loads(template_json)
    except:
        template = {}

    # Validate and save all uploaded files
    saved_paths = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            saved_paths.append(filepath)

    if not saved_paths:
        return jsonify({'error': 'Invalid file type'}), 400

    primary_path = saved_paths[0]

    # Build the generation prompt
    placement = template.get('placement', 'naturally on the model')
    size_hint = template.get('size_hint', '')
    pose = template.get('model_pose', 'natural elegant pose')
    template_name = template.get('name', category)
    set_instruction = template.get('set_instruction', '')
    pieces = template.get('pieces', [])

    # Detect if this is a jewellery set category
    cats = load_categories()
    cat_data = cats.get(category, {})
    is_set = cat_data.get('is_set', False) or 'set' in category.lower()

    if is_set:
        # SET PROMPT — explicitly requires ALL pieces from the reference image
        pieces_desc = ' and '.join(pieces) if pieces else 'all pieces in the set'
        prompt = f"""Professional high-end fashion photography. A beautiful model wearing this complete jewellery set.

⚠ THIS IS A JEWELLERY SET — MULTIPLE PIECES MUST ALL BE WORN SIMULTANEOUSLY ⚠

The reference image shows a COMPLETE SET. You must place ALL pieces on the model:
{set_instruction if set_instruction else f'Show all pieces from the reference image ({pieces_desc}) worn together on the model at the same time.'}

Pieces to include: {pieces_desc if pieces else 'every piece shown in the reference image'}
Placement: {placement}
Size and proportion: {size_hint}
Model pose: {pose}
Style template: {template_name}
Model type: {model_preference}

CRITICAL RULES FOR SETS:
1. EVERY piece from the reference image must appear on the model — do not omit any piece.
2. Reproduce each piece with IDENTICAL design — same stones, same color (rose gold/silver/gold), same pattern, same shape.
3. If the set has a necklace AND earrings, BOTH must be visible on the model at the same time.
4. The necklace goes on the neck/chest. The earrings go on BOTH ears. Each piece in its correct anatomical location.
5. Match the reference image exactly: same gemstone colors (e.g. pink/purple stones, green drops), same metal tone, same design motifs.
6. Studio lighting that reveals all pieces clearly. Hair fully pinned up to expose ears.
7. High-end jewellery catalogue quality — every piece sharp and detailed.
{f'Additional notes: {custom_prompt}' if custom_prompt else ''}
{f'Avoid: {negative_prompt}' if negative_prompt else ''}"""

    else:
        # SINGLE PIECE PROMPT (original logic)
        # Hard duplication guard — injected as a locked prefix when the feedback loop
        # detected that the model rendered the jewellery more than once.
        duplication_prefix = ""
        if duplication_guard:
            duplication_prefix = f"""⚠ ABSOLUTE CONSTRAINT — READ BEFORE ANYTHING ELSE ⚠
The previous generation rendered TWO {category} pieces on the model. This is WRONG.
Common failure modes to AVOID: one piece at choker height AND another piece lower on the chest; one tight piece AND one looser piece; any two pieces of jewellery that look like they could be separate items.
YOU MUST PLACE EXACTLY ONE (1) {category} ON THE MODEL. ONE piece. ONE location. ONE height. Not two. Not three. ONE.
If you place more than one {category} anywhere on the model, the output is rejected.
This constraint overrides every other instruction below.
——————————————————————————————————————\n\n"""

        prompt = f"""{duplication_prefix}Professional fashion photography. A beautiful model wearing this exact {category} jewellery piece.

Jewellery placement: {placement}
Size and proportion: {size_hint}
Model pose: {pose}
Style template: {template_name}
Model type: {model_preference}

The jewellery must be prominently visible, correctly sized, and realistically placed. 
Studio lighting, high-end fashion magazine quality.
{f'Additional notes: {custom_prompt}' if custom_prompt else ''}
{f'Avoid: {negative_prompt}' if negative_prompt else ''}

CRITICAL RULES — follow exactly:
1. Reproduce the jewellery from the reference image with IDENTICAL design — same number of strands/stones/chains, same shape, same color.
2. Do NOT add extra strands, layers, chains, or elements that are not in the reference image.
3. Do NOT duplicate or multiply any part of the jewellery.
4. The jewellery piece must appear exactly ONCE on the model, in exactly one location.
5. Only ONE {category} total. Never place two or more pieces of jewellery on the model."""

    result = generate_with_gemini(primary_path, prompt, category, extra_paths=saved_paths[1:])

    return jsonify(result)

def generate_with_gemini(image_path, prompt, category, extra_paths=None):
    log.info(f"[generate-with-gemini] START | category={category} | extra_paths={len(extra_paths or [])}")
    client, err = get_gemini_client()
    if err:
        log.error(f"[generate-with-gemini] FAIL | client error: {err}")
        return {'error': err}

    try:
        # Open primary image
        image = Image.open(image_path)

        # Build contents: prompt, primary image, then any extra angle images
        contents = [prompt, image]
        if extra_paths:
            angle_note = f"\n\nNote: {len(extra_paths)} additional angle(s) of the same jewellery piece are provided below for reference. Use all angles to understand the full design before generating."
            contents[0] = prompt + angle_note
            for ep in extra_paths:
                try:
                    contents.append(Image.open(ep))
                except Exception:
                    pass  # skip unreadable extra images

        # Use the image editing model — text-and-image-to-image
        response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"]
            )
        )

        # Extract generated image from response parts
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                out_filename = f"output_{uuid.uuid4()}.png"
                out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_filename)
                # Save via PIL
                generated = Image.open(io.BytesIO(part.inline_data.data))
                generated.save(out_path)
                return {
                    'success': True,
                    'image_url': f'/static/outputs/{out_filename}',
                    'provider': 'Gemini Image Editing',
                    'prompt_used': prompt
                }

        # If no image part returned, surface any text for debugging
        text_parts = [p.text for p in response.candidates[0].content.parts if p.text]
        return {
            'error': 'Gemini returned no image. The model may have declined the request.',
            'details': ' '.join(text_parts) if text_parts else 'No details returned.'
        }

    except Exception as e:
        err_str = str(e)
        if '429' in err_str or 'quota' in err_str.lower():
            hint = 'You have hit the API rate limit. Wait a moment and try again.'
        elif '403' in err_str or 'permission' in err_str.lower():
            hint = 'Your API key does not have access to the image generation model. Make sure you are using a Gemini API key from Google AI Studio (aistudio.google.com).'
        else:
            hint = 'Check that your GEMINI_API_KEY is valid and has image generation enabled.'
        return {'error': f'Gemini failed: {err_str}', 'details': hint}

@app.route('/api/analyze-result', methods=['POST'])
def analyze_result():
    data = request.json
    original_src = data.get('original_src', '')  # base64 data URL
    generated_url = data.get('generated_url', '')
    category = data.get('category', '')
    template = data.get('template', {})
    current_prompt = data.get('current_prompt', '')
    current_negative = data.get('current_negative', '')

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    try:
        # Load generated image from disk
        gen_filename = generated_url.split('/static/outputs/')[-1].split('?')[0]
        gen_path = os.path.join(app.config['OUTPUT_FOLDER'], gen_filename)
        gen_image = Image.open(gen_path)

        # Decode original from base64 data URL
        if ',' in original_src:
            b64data = original_src.split(',', 1)[1]
        else:
            b64data = original_src
        orig_image = Image.open(io.BytesIO(base64.b64decode(b64data)))

        # Detect set category for analysis
        cats_data = load_categories()
        cat_info = cats_data.get(category, {})
        is_set_category = cat_info.get('is_set', False) or 'set' in category.lower()
        set_pieces = template.get('pieces', [])
        set_note = ""
        if is_set_category:
            pieces_str = ' and '.join(set_pieces) if set_pieces else 'all pieces in the set'
            set_note = f"""
⚠ SET CATEGORY NOTE: This is a "{category}" — a complete jewellery SET.
The reference image contains MULTIPLE pieces ({pieces_str}) that are ALL supposed to appear on the model simultaneously.
For sets, seeing a necklace AND earrings together is CORRECT and NOT duplication.
Duplication means the SAME piece appearing more than once (e.g., two necklaces at different heights, or earrings appearing four times).
The set instruction was: {template.get('set_instruction', 'Show all pieces together.')}
"""

        analysis_prompt = f"""You are a professional AI image-generation prompt engineer specialising in jewellery photography.

You are given two images:
- Image 1: The ORIGINAL jewellery piece (source of truth)
- Image 2: The AI-GENERATED model photo that tried to reproduce it

Category: {category}
Template: {template.get('name', '')} — {template.get('placement', '')}
{set_note}

════════════════════════════════════════
STEP 1 — DUPLICATION CHECK (do this first)
════════════════════════════════════════
{"This is a SET category. Check: are ALL required pieces present (necklace on neck, earrings on both ears, etc.)? A necklace + earrings together is CORRECT for a set. Duplication means the SAME individual piece appears more than once in the wrong way (e.g., two necklaces at different heights, or earrings appearing four times). Missing pieces are a bigger concern than duplication for sets." if is_set_category else "Does the jewellery piece appear MORE THAN ONCE on the model in Image 2? (e.g. two necklaces at different heights, ring on two fingers, bracelet on both wrists) If YES → this is the PRIMARY issue. List it first."}

════════════════════════════════════════
STEP 2 — DESCRIBE ORIGINAL (Image 1) using SPATIAL geometry, not strand-index order
════════════════════════════════════════
Describe using these spatial axes:
- CENTER: what sits at the exact front-center of the piece?
- SIDES (LEFT/RIGHT of center): what elements appear on each side, and how far out?
- TOWARD BACK/CLASP: what happens as the piece curves toward the back?
- OVERALL STRUCTURE: how many strands/rows/layers total?
- BEAD/STONE DETAILS: exact shape (round, oval, elongated, faceted?), texture (smooth, textured, granulated?), size relative to strand
- CLASP: style and position

════════════════════════════════════════
STEP 3 — DESCRIBE GENERATED (Image 2) using the same spatial framework
════════════════════════════════════════

════════════════════════════════════════
STEP 4 — LIST ALL DISCREPANCIES
════════════════════════════════════════
Compare spatially. List every mismatch including: duplication, wrong bead shape/texture, wrong cluster positions, wrong strand count, wrong sizing, missing details.

════════════════════════════════════════
STEP 5 — WRITE THE REFINED PROMPT
════════════════════════════════════════
Write a refined_prompt using the GEOMETRY-FIRST FORMAT proven to work for AI image generators.
Follow this exact structure (adapt content to the actual jewellery):

"Ultra-realistic studio product photograph of a SINGLE [metal] [category] worn on a female model.
[STRICT CROP / FRAMING — HARD CONSTRAINT]
- Frame from lower neck/wrist/hand to upper chest ONLY (adapt to {category})
- Necklace/piece centered horizontally and vertically
[JEWELRY — EXACT GEOMETRY, DO NOT ALTER]
- One single piece (no layering, no duplicates, no second piece)
- [exact strand/layer count] [material] [structure description]
- Piece sits [exact placement on body]
[ELEMENT STRUCTURE — LOCK THIS]
- CENTER: [exact center element description — count, shape, arrangement]
- SIDES: [what appears on both sides of center, mirrored]
- TOWARD BACK: [what the piece does as it curves back — plain wire? more elements?]
[SYMMETRY RULE — ENFORCE]
- Left and right sides must be perfectly mirrored
[MODEL]
- [skin tone from image], realistic skin texture
- Hair fully tied back or completely out of frame
[LIGHTING]
- Soft diffused studio lighting, sharp focus on jewelry
[STYLE]
- E-commerce jewelry product photography, highly detailed, sharp, realistic [metal] texture"

════════════════════════════════════════
STEP 6 — WRITE THE REFINED NEGATIVE PROMPT
════════════════════════════════════════
List the specific wrong things found in Image 2 as comma-separated terms.
ALWAYS include: "two {category.lower()}s, duplicate jewellery, second piece, layered necklace, jewellery appearing twice"
Then add the specific wrongs found: wrong bead shapes, wrong textures, wrong counts, etc.

════════════════════════════════════════
Return ONLY this JSON object (no markdown, no explanation):
{{
  "original_description": "spatial geometry description from Step 2",
  "generated_description": "spatial geometry description from Step 3",
  "issues": ["issue 1", "issue 2", ...],
  "refined_prompt": "full geometry-first prompt from Step 5",
  "refined_negative": "comma-separated negative terms from Step 6"
}}"""

        # Convert PIL images to bytes for the SDK
        def pil_to_part(img):
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return types.Part.from_bytes(data=buf.getvalue(), mime_type='image/png')

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[analysis_prompt, pil_to_part(orig_image), pil_to_part(gen_image)]
        )

        text = response.text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        result = json.loads(text)

        # Post-process: detect duplication from issues list and descriptions
        # For set categories, skip duplication detection (earrings+necklace together is intentional)
        cats_data2 = load_categories()
        cat_info2 = cats_data2.get(category, {})
        is_set2 = cat_info2.get('is_set', False) or 'set' in category.lower()

        issues_text = ' '.join(result.get('issues', [])).lower()
        gen_desc = result.get('generated_description', '').lower()
        orig_desc = result.get('original_description', '').lower()
        combined_text = issues_text + ' ' + gen_desc
        duplication_keywords = [
            'two ', 'twice', 'double', 'duplicate', 'appears more than once',
            'multiple ', 'extra necklace', 'extra ring', 'extra earring', 'extra bracelet',
            'appearing twice', 'rendered twice', 'two necklaces', 'two rings', 'two bracelets',
            'second necklace', 'second ring', 'different heights', 'two levels', 'two layers',
            'choker and', 'and a longer', 'layered over', 'stacked with', 'two pieces',
            'both a ', 'as well as a', 'in addition to', 'another necklace', 'plus a'
        ]
        duplication_detected = (not is_set2) and any(kw in combined_text for kw in duplication_keywords)
        result['duplication_detected'] = duplication_detected

        # When duplication is found, force the negative prompt to include strong duplication terms
        if duplication_detected:
            base_negative = result.get('refined_negative', '')
            duplication_negative_terms = (
                "two necklaces, double necklace, duplicate jewellery, two pieces, "
                "multiple jewellery items, jewellery appearing twice, second piece, "
                "extra layer, duplicated chain, two rings, mirrored jewellery"
            )
            if duplication_negative_terms not in base_negative:
                result['refined_negative'] = duplication_negative_terms + (', ' + base_negative if base_negative else '')
            # Prepend a hard single-piece constraint to the refined positive prompt too
            refined = result.get('refined_prompt', '')
            single_piece_prefix = "SINGLE PIECE ONLY — one jewellery item, worn once, at one location on the body. "
            if not refined.startswith("SINGLE PIECE ONLY"):
                result['refined_prompt'] = single_piece_prefix + refined

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


def generate_with_stability(image_path, prompt):
    api_key = os.getenv('STABILITY_API_KEY')
    if not api_key:
        return {'error': 'STABILITY_API_KEY not set in .env file'}

    try:
        with open(image_path, 'rb') as f:
            image_data = f.read()

        response = requests.post(
            "https://api.stability.ai/v2beta/stable-image/generate/core",
            headers={
                "authorization": f"Bearer {api_key}",
                "accept": "image/*"
            },
            files={"none": ''},
            data={
                "prompt": prompt,
                "output_format": "webp",
                "aspect_ratio": "2:3",
            }
        )

        if response.status_code == 200:
            out_filename = f"output_{uuid.uuid4()}.webp"
            out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_filename)
            with open(out_path, 'wb') as f:
                f.write(response.content)
            return {
                'success': True,
                'image_url': f'/static/outputs/{out_filename}',
                'provider': 'Stability AI',
                'prompt_used': prompt
            }
        else:
            return {'error': f'Stability AI error: {response.text}'}
    except Exception as e:
        return {'error': f'Stability AI failed: {str(e)}'}


@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'files' not in request.files:
        log.warning("[upload] FAIL | no files in request")
        return jsonify({'error': 'No files provided'}), 400
    files = request.files.getlist('files')
    log.info(f"[upload] received {len(files)} file(s)")
    uploaded = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            size_kb = os.path.getsize(filepath) / 1024
            uploaded.append({'filename': filename, 'url': f'/static/uploads/{filename}'})
            log.info(f"[upload] saved: {filename} ({size_kb:.1f}KB)")
        else:
            log.warning(f"[upload] skipped invalid file: {getattr(file, 'filename', 'unknown')}")
    if not uploaded:
        log.warning("[upload] FAIL | no valid image files in request")
        return jsonify({'error': 'No valid image files uploaded'}), 400
    log.info(f"[upload] SUCCESS | {len(uploaded)} file(s) saved")
    return jsonify({'success': True, 'files': uploaded})


@app.route('/api/generate-both', methods=['POST'])
def generate_both():
    """Mobile app endpoint — returns a product shot AND a model shot in one call."""
    t0 = time.time()
    try:
        data = request.json
        filenames = data.get('filenames', [])
        category = data.get('category', 'Jewellery')
        template = data.get('template', {})
        model_pref = data.get('modelPref', 'diverse female model')
        custom_prompt = data.get('customPrompt', '')
        negative_prompt = data.get('negativePrompt', '')

        log.info(f"[generate-both] START | category={category} | template={template.get('name','?')} | model={model_pref} | files={filenames}")

        if not filenames:
            log.warning("[generate-both] FAIL | no filenames provided")
            return jsonify({'error': 'No image filenames provided'}), 400

        client, err = get_gemini_client()
        if err:
            log.error(f"[generate-both] FAIL | gemini client error: {err}")
            return jsonify({'error': err}), 500

        # Load and resize all uploaded images
        images = []
        for fn in filenames:
            fp = os.path.join(app.config['UPLOAD_FOLDER'], fn)
            if os.path.exists(fp):
                img = Image.open(fp)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                if max(img.size) > 1024:
                    img.thumbnail((1024, 1024), Image.LANCZOS)
                images.append(img)
                log.info(f"[generate-both] loaded image: {fn} ({img.size})")
            else:
                log.warning(f"[generate-both] image not found on disk: {fn}")

        if not images:
            log.error("[generate-both] FAIL | no valid images loaded from disk")
            return jsonify({'error': 'No valid images found on server'}), 400

        def pil_to_part(img):
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return types.Part.from_bytes(data=buf.getvalue(), mime_type='image/png')

        placement = template.get('placement', f'correctly placed on body')
        size_hint = template.get('size_hint', 'standard size')
        model_pose = template.get('model_pose', 'natural pose')
        template_name = template.get('name', category)

        # ── Prompt 1: E-commerce white background product shot ──────────────
        product_prompt = f"""Ultra-realistic e-commerce product photograph of this {category}.
- Pure white background (#FFFFFF), clean studio setup
- Floating flat-lay or elegant prop presentation — NOT worn on a human body
- Soft diffused studio lighting, no harsh shadows
- Sharp focus on every detail of the jewellery
- High resolution, commercial product photography quality
- Show the full design from the best display angle
- Accurate reproduction of shape, materials, texture and design
- Professional product shot as seen on luxury e-commerce websites
{('Additional: ' + custom_prompt) if custom_prompt else ''}
{('Avoid: ' + negative_prompt) if negative_prompt else ''}"""

        # ── Prompt 2: Model shot ─────────────────────────────────────────────
        model_prompt = f"""Ultra-realistic fashion photograph of a {model_pref} wearing this exact {category} ({template_name} style).
Placement: {placement}
Size: {size_hint}
Pose: {model_pose}
- Realistic skin texture, professional model, hair styled to fully reveal the jewellery
- High-end fashion/editorial lighting, sharp focus on the jewellery
- The jewellery must match the reference images exactly — same design, same materials
- E-commerce model photography quality
{('Additional: ' + custom_prompt) if custom_prompt else ''}
{('Avoid: ' + negative_prompt) if negative_prompt else 'Avoid: blurry jewellery, distorted proportions, extra limbs, wrong placement'}"""

        results = {'success': True}

        # Generate product shot
        try:
            log.info(f"[generate-both] calling Gemini for PRODUCT SHOT | model=gemini-2.5-flash-image")
            t1 = time.time()
            contents = [product_prompt] + [pil_to_part(img) for img in images]
            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
            )
            parts = response.candidates[0].content.parts
            text_parts = [p.text for p in parts if getattr(p, 'text', None)]
            img_parts  = [p for p in parts if getattr(p, 'inline_data', None)]
            log.info(f"[generate-both] product shot response | {time.time()-t1:.1f}s | text_parts={len(text_parts)} img_parts={len(img_parts)}")
            if text_parts:
                log.info(f"[generate-both] product shot Gemini text: {' '.join(text_parts)[:500]}")
            for part in img_parts:
                out_fn = f"product_{uuid.uuid4()}.png"
                out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                Image.open(io.BytesIO(part.inline_data.data)).save(out_path)
                results['product_shot_url'] = f'/static/outputs/{out_fn}'
                log.info(f"[generate-both] product shot saved: {out_fn}")
                break
            if not results.get('product_shot_url'):
                log.warning(f"[generate-both] product shot — Gemini returned no image part")
        except Exception as e:
            log.error(f"[generate-both] product shot EXCEPTION: {e}", exc_info=True)
            results['product_shot_error'] = str(e)

        # Generate model shot
        try:
            log.info(f"[generate-both] calling Gemini for MODEL SHOT | model=gemini-2.5-flash-image")
            t2 = time.time()
            contents = [model_prompt] + [pil_to_part(img) for img in images]
            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
            )
            parts = response.candidates[0].content.parts
            text_parts = [p.text for p in parts if getattr(p, 'text', None)]
            img_parts  = [p for p in parts if getattr(p, 'inline_data', None)]
            log.info(f"[generate-both] model shot response | {time.time()-t2:.1f}s | text_parts={len(text_parts)} img_parts={len(img_parts)}")
            if text_parts:
                log.info(f"[generate-both] model shot Gemini text: {' '.join(text_parts)[:500]}")
            for part in img_parts:
                out_fn = f"model_{uuid.uuid4()}.png"
                out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                Image.open(io.BytesIO(part.inline_data.data)).save(out_path)
                results['model_shot_url'] = f'/static/outputs/{out_fn}'
                log.info(f"[generate-both] model shot saved: {out_fn}")
                break
            if not results.get('model_shot_url'):
                log.warning(f"[generate-both] model shot — Gemini returned no image part")
        except Exception as e:
            log.error(f"[generate-both] model shot EXCEPTION: {e}", exc_info=True)
            results['model_shot_error'] = str(e)

        if not results.get('product_shot_url') and not results.get('model_shot_url'):
            log.error(f"[generate-both] FAIL — both shots missing | total={time.time()-t0:.1f}s")
            return jsonify({'error': 'Gemini returned no images. Check your API key permissions.'}), 500

        log.info(f"[generate-both] SUCCESS | product={'yes' if results.get('product_shot_url') else 'no'} model={'yes' if results.get('model_shot_url') else 'no'} | total={time.time()-t0:.1f}s")
        return jsonify(results)

    except Exception as e:
        log.error(f"[generate-both] UNHANDLED EXCEPTION: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def _normalize_price_to_inr(price_str):
    """Convert foreign currency prices to INR approximation, or return as-is with flag."""
    if not price_str:
        return None
    price_str = price_str.strip()
    # Already INR
    if 'Rs' in price_str or '₹' in price_str or 'INR' in price_str:
        return price_str
    # USD → INR (approx 83x)
    usd_match = re.search(r'\$\s*([\d,]+\.?\d*)', price_str)
    if usd_match:
        usd = float(usd_match.group(1).replace(',', ''))
        inr = int(usd * 83)
        return f'₹{inr:,} (~${usd_match.group(1)})'
    # GBP → INR (approx 105x)
    gbp_match = re.search(r'£\s*([\d,]+\.?\d*)', price_str)
    if gbp_match:
        gbp = float(gbp_match.group(1).replace(',', ''))
        inr = int(gbp * 105)
        return f'₹{inr:,} (~£{gbp_match.group(1)})'
    # EUR → INR (approx 90x)
    eur_match = re.search(r'€\s*([\d,]+\.?\d*)', price_str)
    if eur_match:
        eur = float(eur_match.group(1).replace(',', ''))
        inr = int(eur * 90)
        return f'₹{inr:,} (~€{eur_match.group(1)})'
    return price_str


def _fetch_og_thumbnail(url):
    """
    Scrape og:image from a listing page. Designed to fail fast:
    - 1.5s connect+read timeout (combined)
    - No retries, no redirects beyond 1 hop
    - Returns None immediately on any error
    """
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            timeout=(1.0, 1.5),   # (connect_timeout, read_timeout)
            headers={'User-Agent': 'Mozilla/5.0 (compatible; JewelleryBot/1.0)'},
            allow_redirects=True,
            stream=True,           # don't download the full page body upfront
        )
        if resp.status_code != 200:
            resp.close()
            return None
        # Read only the first 8KB — og:image is always in <head>
        chunk = next(resp.iter_content(8192), b'')
        resp.close()
        text = chunk.decode('utf-8', errors='ignore')
        og_match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\'>]+)',
            text, re.IGNORECASE
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\'>]+)["\'][^>]+property=["\']og:image',
            text, re.IGNORECASE
        )
        if og_match:
            img_url = og_match.group(1).strip()
            if img_url.startswith('//'):
                img_url = 'https:' + img_url
            if img_url.startswith('http'):
                return img_url
    except Exception:
        pass   # timeout, SSL error, redirect loop — all silently dropped
    return None


def _enrich_listing(item):
    """Normalize price and fetch thumbnail for one listing (runs in thread pool)."""
    normalized_price = _normalize_price_to_inr(item.get('price'))
    thumb = item.get('thumbnail')
    if not thumb and item.get('url'):
        thumb = _fetch_og_thumbnail(item['url'])
    return {
        'title':     item.get('title', ''),
        'url':       item.get('url', ''),
        'source':    item.get('source', ''),
        'price':     normalized_price,
        'thumbnail': thumb,
    }


def _enrich_listing_no_thumb(item):
    """Price normalisation only — used as fallback when thumbnail fetch timed out."""
    return {
        'title':     item.get('title', ''),
        'url':       item.get('url', ''),
        'source':    item.get('source', ''),
        'price':     _normalize_price_to_inr(item.get('price')),
        'thumbnail': None,
    }


@app.route('/api/market-research', methods=['POST'])
def api_market_research():
    """
    Mobile app endpoint — analyses a jewellery photo and returns
    similar listings, keywords, price range, and a market summary
    using Gemini with Google Search grounding.
    """
    category = request.form.get('category', 'Jewellery')
    keyword  = request.form.get('keyword', '').strip() or None
    image    = request.files.get('image')

    log.info(f"[market-research] START | category={category} | keyword={keyword}")

    if not image:
        log.warning("[market-research] FAIL | no image provided")
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    client, err = get_gemini_client()
    if err:
        log.error(f"[market-research] FAIL | gemini client error: {err}")
        return jsonify({'success': False, 'error': err}), 500

    try:
        img_bytes = image.read()
        log.info(f"[market-research] image read | size={len(img_bytes)/1024:.1f}KB")
        filter_note = f'Focus results specifically on listings matching: "{keyword}".' if keyword else ''

        prompt = f"""You are a jewellery market research expert helping an Indian jewellery seller understand their competition.

Carefully analyse this {category} image — note the style, material, gemstones, finish, and design motifs.
Then use Google Search to find at least 10 similar items currently being sold online.

{filter_note}

Respond ONLY with this exact JSON — no markdown, no code fences, no extra text:
{{
  "keywords": ["keyword1", "keyword2"],
  "summary": "3-4 sentence market analysis covering: how many sellers, price range, main platforms, and 1 actionable insight for the seller.",
  "listings": [
    {{
      "title": "Exact product title from the listing",
      "url": "https://full-listing-url.com/product-page",
      "source": "domain.com",
      "price": "₹1,200",
      "thumbnail": "https://cdn.domain.com/product-image.jpg"
    }}
  ],
  "price_range": {{ "min": "₹500", "max": "₹5,000" }}
}}

STRICT RULES — follow exactly:
1. keywords: exactly 8-10 SHORT tags (2-3 words max each) — material, style, stone, occasion, finish
2. listings: find AT LEAST 10 real live listings — use multiple searches if needed
   - Prioritise: Amazon.in, Flipkart, Myntra, Meesho, Nykaa, BlueStone, CaratLane, Tanishq, Craftsvilla, Etsy India
   - Each listing must have a real, working URL
   - price: ALWAYS use ₹ / Rs. format — convert foreign currency to INR (1 USD ≈ ₹83, 1 GBP ≈ ₹105)
   - thumbnail: use the direct product image CDN URL from the listing (NOT the page URL). Look for image URLs ending in .jpg/.png/.webp in the search results. If not available, use null — do NOT make up image URLs.
3. price_range: min and max from the actual listings you found, in ₹
4. summary: mention the total number of sellers/listings found, the price spread, which platforms dominate, and whether this is a competitive or niche segment"""

        log.info(f"[market-research] calling Gemini with Google Search grounding…")
        t_gemini = time.time()
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'),
                prompt,
            ],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )
        log.info(f"[market-research] Gemini responded in {time.time()-t_gemini:.1f}s")

        raw = response.text.strip().replace('```json', '').replace('```', '').strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return jsonify({'success': False, 'error': 'Gemini did not return valid JSON. Raw: ' + raw[:300]}), 500

        parsed = json.loads(match.group(0))
        listings = parsed.get('listings', [])

        # Enrich listings in parallel (price normalisation + thumbnail fetch).
        # Hard wall: 8s total for ALL thumbnails combined — we'd rather show
        # listings without images than stall the whole response.
        enriched = [None] * len(listings)
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_enrich_listing, item): i for i, item in enumerate(listings)}
            try:
                for future in as_completed(futures, timeout=8):
                    idx = futures[future]
                    try:
                        enriched[idx] = future.result(timeout=3)
                    except Exception:
                        enriched[idx] = _enrich_listing_no_thumb(listings[idx])
            except Exception:
                # as_completed timed out — fill remaining slots without thumbnails
                for future, idx in futures.items():
                    if enriched[idx] is None:
                        enriched[idx] = _enrich_listing_no_thumb(listings[idx])
        enriched = [e for e in enriched if e]

        log.info(f"[market-research] SUCCESS | listings={len(enriched)} | keywords={len(parsed.get('keywords',[]))} | price_range={parsed.get('price_range')}")
        return jsonify({
            'success':      True,
            'keywords':     parsed.get('keywords', []),
            'summary':      parsed.get('summary', ''),
            'listings':     enriched,
            'seller_count': len(enriched),
            'price_range':  parsed.get('price_range'),
        })

    except Exception as e:
        log.error(f"[market-research] EXCEPTION: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/static/outputs/<filename>')
def output_file(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename)

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
    port = int(os.getenv('PORT', 5050))
    debug = not _IS_RAILWAY
    app.run(host='0.0.0.0', debug=debug, port=port)
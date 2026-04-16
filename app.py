import os, json, base64, uuid, re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from PIL import Image
import io
from dotenv import load_dotenv
import requests

load_dotenv()

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['OUTPUT_FOLDER'] = 'static/outputs'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

CATEGORIES_FILE = 'categories.json'

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
    client = genai.Client(api_key=api_key)
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
            model="gemini-2.0-flash",
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
    if 'jewellery_image' not in request.files:
        return jsonify({'error': 'No image uploaded'}), 400

    file = request.files['jewellery_image']
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

    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type'}), 400

    # Save uploaded file
    filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

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

    result = generate_with_gemini(filepath, prompt, category)

    return jsonify(result)

def generate_with_gemini(image_path, prompt, category):
    client, err = get_gemini_client()
    if err:
        return {'error': err}

    try:
        # Open image with PIL (new SDK accepts PIL images directly)
        image = Image.open(image_path)

        # Use the image editing model — text-and-image-to-image
        response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=[prompt, image],
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
            model="gemini-2.0-flash",
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


@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/static/outputs/<filename>')
def output_file(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename)

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
    app.run(debug=True, port=5050)
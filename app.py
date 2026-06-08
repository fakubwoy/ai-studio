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

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('atelier')

app = Flask(__name__)
CORS(app)

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
CATEGORIES_FILE = os.path.join(_TMP, 'categories.json')

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
    return genai.Client(api_key=api_key), None

def get_meshy_key():
    key = os.getenv('MESHY_API_KEY')
    if not key:
        return None, "MESHY_API_KEY not set in .env file"
    return key, None

# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route('/')
def landing():
    return render_template('landing.html', active_nav='')

@app.route('/studio/sketch')
def page_sketch():
    cats = load_categories()
    return render_template('sketch.html', categories=list(cats.keys()), active_nav='sketch')

@app.route('/studio/model')
def page_model():
    cats = load_categories()
    return render_template('model.html', categories=list(cats.keys()), active_nav='model')

@app.route('/studio/cad')
def page_cad():
    cats = load_categories()
    return render_template('cad.html', categories=list(cats.keys()), active_nav='cad')

@app.route('/studio/market')
def page_market():
    cats = load_categories()
    return render_template('market.html', categories=list(cats.keys()), active_nav='market')

@app.route('/studio/gallery')
def page_gallery():
    return render_template('gallery.html', active_nav='gallery')

# ── API: Categories ────────────────────────────────────────────────────────────

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

# ── API: Sketch Conceptualiser ─────────────────────────────────────────────────

@app.route('/api/conceptualise', methods=['POST'])
def api_conceptualise():
    """
    Takes a sketch image (optional) + text description + variations list.
    Returns multiple concept objects with image_url, title, description, tags.
    """
    prompt_text  = request.form.get('prompt', '').strip()
    category     = request.form.get('category', 'Jewellery')
    metal        = request.form.get('metal', '22K Yellow Gold')
    variations   = json.loads(request.form.get('variations', '["Classic"]'))
    sketch_data  = request.form.get('sketch_data', '')  # base64 data URL

    if not prompt_text and not sketch_data:
        return jsonify({'error': 'Provide a description or upload a sketch.'}), 400

    client, err = get_gemini_client()
    if err:
        return jsonify({'error': err}), 500

    concepts = []
    errors = []

    for variation in variations:
        try:
            gen_prompt = f"""You are a senior jewellery design artist. 
{"The user has provided a rough sketch of their jewellery concept." if sketch_data else ""}
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
{"Interpret and improve the rough sketch, maintaining its core design intent." if sketch_data else ""}"""

            contents = [gen_prompt]
            if sketch_data and ',' in sketch_data:
                b64 = sketch_data.split(',', 1)[1]
                img_bytes = base64.b64decode(b64)
                img = Image.open(io.BytesIO(img_bytes))
                contents.append(img)

            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
            )

            image_url = None
            for part in response.candidates[0].content.parts:
                if part.inline_data is not None:
                    out_fn = f"concept_{uuid.uuid4()}.png"
                    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                    Image.open(io.BytesIO(part.inline_data.data)).save(out_path)
                    image_url = f'/static/outputs/{out_fn}'
                    break

            # Generate metadata with a text call
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

            concepts.append({
                'variation': variation,
                'image_url': image_url,
                'title': meta.get('title', f'{variation} {category}'),
                'description': meta.get('description', ''),
                'tags': meta.get('tags', []),
            })

            # Save to output gallery metadata
            if image_url:
                log.info(f"[conceptualise] saved concept: {image_url} | variation={variation}")

        except Exception as e:
            log.error(f"[conceptualise] FAIL for variation={variation}: {e}", exc_info=True)
            errors.append(f"{variation}: {str(e)}")

    if not concepts:
        return jsonify({'error': 'All variations failed. ' + '; '.join(errors)}), 500

    return jsonify({'concepts': concepts, 'errors': errors if errors else None})

# ── API: Model Image Generation (existing logic, cleaned up) ───────────────────

@app.route('/api/generate-image', methods=['POST'])
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
                out_fn = f"output_{uuid.uuid4()}.png"
                out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_fn)
                Image.open(io.BytesIO(part.inline_data.data)).save(out_path)
                return {'success': True, 'image_url': f'/static/outputs/{out_fn}', 'provider': 'Gemini', 'prompt_used': prompt}

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

@app.route('/api/generate-cad', methods=['POST'])
def api_generate_cad():
    """Submit an image to Meshy AI image-to-3D and return a task_id for polling."""
    image_data = request.form.get('image_data', '')
    prompt     = request.form.get('prompt', '').strip()
    art_style  = request.form.get('art_style', 'realistic')
    target_use = request.form.get('target_use', 'visualization')

    meshy_key, err = get_meshy_key()
    if err:
        return jsonify({'error': err, 'details': 'Add MESHY_API_KEY to your .env file. Get one free at meshy.ai'}), 500

    if not image_data:
        return jsonify({'error': 'No image provided'}), 400

    # Meshy expects a public URL or base64 string.
    # We'll upload to a temp endpoint on our own server first.
    try:
        if ',' in image_data:
            b64 = image_data.split(',', 1)[1]
        else:
            b64 = image_data

        img_bytes = base64.b64decode(b64)

        # Save locally and use our own static URL
        tmp_fn = f"cad_input_{uuid.uuid4()}.png"
        tmp_path = os.path.join(app.config['UPLOAD_FOLDER'], tmp_fn)
        Image.open(io.BytesIO(img_bytes)).save(tmp_path)

        # Build the image URL that Meshy can reach (must be public in production)
        host = request.host_url.rstrip('/')
        image_url = f"{host}/static/uploads/{tmp_fn}"

        # Meshy art style mapping
        style_map = {'realistic': 'realistic', 'jewelry': 'realistic', 'sculpture': 'sculpture', 'game': 'cartoon'}
        meshy_style = style_map.get(art_style, 'realistic')

        payload = {
            "image_url": image_url,
            "enable_pbr": True,
            "should_remesh": True,
        }
        if prompt:
            payload["object_prompt"] = prompt

        headers = {
            "Authorization": f"Bearer {meshy_key}",
            "Content-Type": "application/json",
        }

        log.info(f"[cad] submitting to Meshy | image_url={image_url}")
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
        # If running locally without internet access to Meshy, return a helpful demo error
        return jsonify({
            'error': 'Cannot reach Meshy API.',
            'details': 'Ensure MESHY_API_KEY is set and this server is accessible from the internet (Meshy needs to reach your image URL). For local testing, deploy to Railway or use ngrok.'
        }), 500
    except Exception as e:
        log.error(f"[cad] exception: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/cad-status/<task_id>', methods=['GET'])
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
        return jsonify({
            'success': True,
            'keywords': parsed.get('keywords', []),
            'summary': parsed.get('summary', ''),
            'listings': enriched,
            'seller_count': len(enriched),
            'price_range': parsed.get('price_range'),
        })

    except Exception as e:
        log.error(f"[market-research] exception: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# ── Static file serving ────────────────────────────────────────────────────────

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/static/outputs/<filename>')
def output_file(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename)

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
    port = int(os.getenv('PORT', 5050))
    debug = not _IS_RAILWAY
    app.run(host='0.0.0.0', debug=debug, port=port)
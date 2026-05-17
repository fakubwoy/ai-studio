/* Jewellery AI Studio – app.js */

let categories = {};
let selectedTemplate = null;
let lastGeneratedUrl = '';
let lastUploadedSrc = '';
let lastPrompt = '';
const gallery = [];

// ── INIT ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadCategories();
  setupTabs();
  setupUploadZone();
  setupDragDrop();
});

// ── TABS ──────────────────────────────────────────────────
function setupTabs() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${tab}`).classList.add('active');
      if (tab === 'categories') renderCategoriesPage();
      if (tab === 'gallery') renderGallery();
    });
  });
}

// ── CATEGORIES ────────────────────────────────────────────
async function loadCategories() {
  const res = await fetch('/api/categories');
  categories = await res.json();
  // Update select options
  const sel = document.getElementById('categorySelect');
  const current = sel.value;
  // Keep default option
  while (sel.options.length > 1) sel.remove(1);
  Object.keys(categories).forEach(cat => {
    const opt = document.createElement('option');
    opt.value = cat; opt.textContent = cat;
    sel.appendChild(opt);
  });
  if (current) sel.value = current;
}

function onCategoryChange() {
  const cat = document.getElementById('categorySelect').value;
  selectedTemplate = null;
  document.getElementById('selectedTemplate').style.display = 'none';

  if (!cat) {
    document.getElementById('templateSection').style.display = 'none';
    return;
  }
  document.getElementById('templateSection').style.display = 'flex';
  renderTemplates(cat);
}

function renderTemplates(cat) {
  const grid = document.getElementById('templateGrid');
  grid.innerHTML = '';
  const templates = (categories[cat] && categories[cat].templates) || [];

  if (templates.length === 0) {
    grid.innerHTML = `<div style="text-align:center;padding:20px;color:var(--text-3);font-size:13px;">
      No templates yet — click <strong>AI Suggest</strong> to generate some.
    </div>`;
    return;
  }

  templates.forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'template-item';
    div.innerHTML = `<span class="ti-name">${t.name}</span><span class="ti-hint">${t.size_hint}</span>`;
    div.addEventListener('click', () => selectTemplate(t, div));
    grid.appendChild(div);
  });
}

function selectTemplate(t, el) {
  document.querySelectorAll('.template-item').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  selectedTemplate = t;
  document.getElementById('tdName').textContent = t.name;
  document.getElementById('tdPlacement').textContent = t.placement;
  document.getElementById('tdSize').textContent = t.size_hint;
  document.getElementById('tdPose').textContent = t.model_pose;
  document.getElementById('selectedTemplate').style.display = 'block';
}

async function suggestTemplates() {
  const cat = document.getElementById('categorySelect').value;
  if (!cat) { showToast('Please select a category first', 'error'); return; }

  const btn = document.getElementById('suggestBtn');
  btn.disabled = true;
  btn.innerHTML = `<span class="ai-spark spinning">✦</span> Generating...`;

  try {
    const res = await fetch('/api/suggest-templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category: cat, description: categories[cat]?.description || '' })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }

    await loadCategories();
    renderTemplates(cat);
    showToast(`✦ ${data.templates.length} templates generated!`, 'success');
  } catch (e) {
    showToast('Failed to suggest templates: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<span class="ai-spark">✦</span> AI Suggest`;
  }
}

// ── MULTI-IMAGE UPLOAD ────────────────────────────────────
let uploadedFiles = []; // array of File objects

function setupUploadZone() {
  const fileInput = document.getElementById('jewelleryFile');
  fileInput.addEventListener('change', e => {
    const newFiles = Array.from(e.target.files);
    newFiles.forEach(f => addUploadedFile(f));
    fileInput.value = ''; // reset so same file can be re-added if needed
  });
}

function addUploadedFile(file) {
  if (uploadedFiles.length >= 6) { showToast('Max 6 images allowed', 'error'); return; }
  // Avoid exact duplicates by name+size
  if (uploadedFiles.find(f => f.name === file.name && f.size === file.size)) return;
  uploadedFiles.push(file);
  renderMultiPreviews();
}

function renderMultiPreviews() {
  const container = document.getElementById('multiImages');
  container.innerHTML = '';
  uploadedFiles.forEach((file, idx) => {
    const wrap = document.createElement('div');
    wrap.className = 'multi-thumb';
    const img = document.createElement('img');
    const reader = new FileReader();
    reader.onload = e => { img.src = e.target.result; };
    reader.readAsDataURL(file);
    const badge = document.createElement('span');
    badge.className = 'multi-thumb-badge';
    badge.textContent = idx === 0 ? 'Primary' : `Angle ${idx + 1}`;
    const rmBtn = document.createElement('button');
    rmBtn.className = 'remove-btn';
    rmBtn.textContent = '✕';
    rmBtn.onclick = (e) => { e.stopPropagation(); removeUploadedFile(idx); };
    wrap.appendChild(img);
    wrap.appendChild(badge);
    wrap.appendChild(rmBtn);
    container.appendChild(wrap);
  });

  const hasFiles = uploadedFiles.length > 0;
  document.getElementById('uploadPlaceholder').style.display = hasFiles ? 'none' : 'flex';
  document.getElementById('uploadMulti').style.display = hasFiles ? 'flex' : 'none';

  // Keep lastUploadedSrc pointing to the primary image (index 0)
  if (uploadedFiles.length > 0) {
    const reader = new FileReader();
    reader.onload = e => { lastUploadedSrc = e.target.result; };
    reader.readAsDataURL(uploadedFiles[0]);
  }
}

function removeUploadedFile(idx) {
  uploadedFiles.splice(idx, 1);
  renderMultiPreviews();
}

function removeImage() {
  uploadedFiles = [];
  renderMultiPreviews();
}

function setupDragDrop() {
  const zone = document.getElementById('uploadZone');
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    Array.from(e.dataTransfer.files).forEach(f => addUploadedFile(f));
  });
  zone.addEventListener('click', e => {
    if (e.target.closest('.remove-btn') || e.target.closest('.btn-outline') || e.target.closest('.multi-add-btn')) return;
    if (uploadedFiles.length === 0) document.getElementById('jewelleryFile').click();
  });
}
// ── GENERATE ──────────────────────────────────────────────
async function generateImage() {
  const category = document.getElementById('categorySelect').value;

  if (!uploadedFiles.length) {
    showToast('Please upload a jewellery image', 'error'); return;
  }
  if (!category) {
    showToast('Please select a jewellery category', 'error'); return;
  }
  if (!selectedTemplate) {
    showToast('Please select a size template', 'error'); return;
  }

  const modelPref = document.querySelector('input[name="modelPref"]:checked').value;
  const customPrompt = document.getElementById('customPrompt').value;
  const negativePrompt = document.getElementById('negativePrompt').value;

  const formData = new FormData();
  // Send all images; backend uses first as primary, rest as additional angles
  uploadedFiles.forEach(f => formData.append('jewellery_image', f));
  formData.append('category', category);
  formData.append('template', JSON.stringify(selectedTemplate));
  formData.append('model_preference', modelPref);
  formData.append('custom_prompt', customPrompt);
  formData.append('negative_prompt', negativePrompt);
  formData.append('duplication_guard', _duplicationGuardActive ? 'true' : 'false');
  _duplicationGuardActive = false;

  // UI state
  const genBtn = document.getElementById('generateBtn');
  genBtn.disabled = true;
  genBtn.innerHTML = `<span class="ai-spark spinning">◈</span> Generating...`;
  document.getElementById('progressArea').style.display = 'flex';
  document.getElementById('outputPlaceholder').style.display = 'none';
  document.getElementById('outputResult').style.display = 'none';
  document.getElementById('outputError').style.display = 'none';

  const steps = [
    [10, 'Analyzing jewellery piece...'],
    [30, 'Identifying design elements...'],
    [55, 'Composing model scene...'],
    [75, 'Applying sizing template...'],
    [90, 'Rendering final image...']
  ];
  let si = 0;
  const progressInterval = setInterval(() => {
    if (si < steps.length) {
      setProgress(steps[si][0], steps[si][1]);
      si++;
    }
  }, 1200);

  try {
    const res = await fetch('/api/generate-image', { method: 'POST', body: formData });
    const data = await res.json();

    clearInterval(progressInterval);
    setProgress(100, 'Complete!');

    setTimeout(() => {
      document.getElementById('progressArea').style.display = 'none';
      if (data.success) {
        document.getElementById('resultImg').src = data.image_url + '?t=' + Date.now();
        document.getElementById('downloadBtn').href = data.image_url;
        document.getElementById('resultMeta').textContent = `Generated with Gemini · ${selectedTemplate.name} template`;
        document.getElementById('outputResult').style.display = 'flex';
        lastPrompt = data.prompt_used || '';
        lastGeneratedUrl = data.image_url;
        // Reset satisfaction UI
        document.getElementById('satisfactionBar').style.display = 'flex';
        document.getElementById('feedbackPanel').style.display = 'none';
        document.getElementById('feedbackResult').style.display = 'none';
        addToGallery(data.image_url, category, selectedTemplate.name);
        showToast('✦ Image generated successfully!', 'success');
      } else {
        document.getElementById('errorText').textContent = data.error || 'Generation failed';
        document.getElementById('outputError').style.display = 'flex';
        document.getElementById('outputPlaceholder').style.display = 'none';
      }
    }, 400);
  } catch (e) {
    clearInterval(progressInterval);
    document.getElementById('progressArea').style.display = 'none';
    document.getElementById('errorText').textContent = 'Network error: ' + e.message;
    document.getElementById('outputError').style.display = 'flex';
  } finally {
    genBtn.disabled = false;
    genBtn.innerHTML = `<span class="gen-icon">◈</span> Generate Image`;
  }
}

function setProgress(pct, text) {
  document.getElementById('progressFill').style.width = pct + '%';
  document.getElementById('progressText').textContent = text;
}

function copyPrompt() {
  if (!lastPrompt) { showToast('No prompt to copy', 'error'); return; }
  navigator.clipboard.writeText(lastPrompt).then(() => showToast('Prompt copied!', 'success'));
}

// ── ADD CATEGORY MODAL ────────────────────────────────────
function showAddCategory() {
  document.getElementById('modalOverlay').classList.add('active');
  document.getElementById('addCategoryModal').classList.add('active');
  document.getElementById('newCatName').focus();
}

async function addCategory() {
  const name = document.getElementById('newCatName').value.trim();
  const desc = document.getElementById('newCatDesc').value.trim();
  if (!name) { showToast('Category name required', 'error'); return; }

  const res = await fetch('/api/categories/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description: desc })
  });
  const data = await res.json();
  if (data.error) { showToast(data.error, 'error'); return; }

  categories = data.categories;
  closeAllModals();
  await loadCategories();

  // Auto-set and suggest templates
  const sel = document.getElementById('categorySelect');
  sel.value = name;
  onCategoryChange();
  showToast(`"${name}" added! Generating AI templates...`, 'success');
  setTimeout(suggestTemplates, 500);

  document.getElementById('newCatName').value = '';
  document.getElementById('newCatDesc').value = '';
}

// ── CATEGORIES PAGE ───────────────────────────────────────
function renderCategoriesPage() {
  const grid = document.getElementById('categoriesGrid');
  grid.innerHTML = '';

  Object.entries(categories).forEach(([name, data]) => {
    const templates = data.templates || [];
    const card = document.createElement('div');
    card.className = 'cat-card';
    card.innerHTML = `
      <div class="cat-card-header">
        <div class="cat-name">${name}</div>
        <div class="cat-badge">${templates.length} templates</div>
      </div>
      <div class="cat-desc">${data.description || '—'}</div>
      <div class="cat-templates">
        ${templates.slice(0, 4).map(t => `<div class="cat-template-item">${t.name} <span style="margin-left:auto;font-size:10px;color:var(--text-3)">${t.size_hint?.split(',')[0] || ''}</span></div>`).join('')}
        ${templates.length > 4 ? `<div style="font-size:11px;color:var(--text-3);padding:4px 0">+${templates.length - 4} more</div>` : ''}
      </div>
      <div class="cat-card-actions">
        <button class="btn-outline" onclick="openTemplateModal('${name}')">Manage Templates</button>
        <button class="btn-ai-suggest" onclick="suggestForCategory('${name}')"><span class="ai-spark">✦</span> AI Add More</button>
      </div>`;
    grid.appendChild(card);
  });
}

async function suggestForCategory(cat) {
  showToast(`Generating templates for ${cat}...`);
  const res = await fetch('/api/suggest-templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ category: cat, description: categories[cat]?.description || '' })
  });
  const data = await res.json();
  if (data.error) { showToast(data.error, 'error'); return; }
  await loadCategories();
  renderCategoriesPage();
  showToast(`✦ ${data.templates.length} templates added to ${cat}!`, 'success');
}

function openTemplateModal(cat) {
  document.getElementById('modalCategoryTitle').textContent = cat + ' — Templates';
  const list = document.getElementById('modalTemplateList');
  list.innerHTML = '';
  const templates = categories[cat]?.templates || [];
  templates.forEach(t => {
    const div = document.createElement('div');
    div.className = 'modal-template-item';
    div.innerHTML = `
      <div class="mti-info">
        <div class="mti-name">${t.name}</div>
        <div class="mti-detail">${t.placement} · ${t.size_hint}</div>
        <div class="mti-detail" style="color:var(--gold-dim)">Pose: ${t.model_pose}</div>
      </div>`;
    list.appendChild(div);
  });
  // Store current category for adding
  document.getElementById('modalTemplateList').dataset.category = cat;
  document.getElementById('modalOverlay').classList.add('active');
  document.getElementById('templateDetailModal').classList.add('active');
}

async function addCustomTemplate() {
  const cat = document.getElementById('modalTemplateList').dataset.category;
  const template = {
    name: document.getElementById('ctName').value.trim(),
    placement: document.getElementById('ctPlacement').value.trim(),
    size_hint: document.getElementById('ctSize').value.trim(),
    model_pose: document.getElementById('ctPose').value.trim()
  };
  if (!template.name) { showToast('Template name required', 'error'); return; }

  const res = await fetch('/api/add-template', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ category: cat, template })
  });
  const data = await res.json();
  if (data.error) { showToast(data.error, 'error'); return; }

  await loadCategories();
  openTemplateModal(cat);
  showToast('Template added!', 'success');
  ['ctName', 'ctPlacement', 'ctSize', 'ctPose'].forEach(id => document.getElementById(id).value = '');
}

// ── SATISFACTION & FEEDBACK ───────────────────────────────
function onSatisfied(yes) {
  document.getElementById('satisfactionBar').style.display = 'none';
  if (yes) {
    showToast('Great! Image saved to gallery ✦', 'success');
    return;
  }
  // Show feedback panel and start analysis
  document.getElementById('feedbackPanel').style.display = 'block';
  document.getElementById('feedbackAnalyzing').style.display = 'flex';
  document.getElementById('feedbackResult').style.display = 'none';
  analyzeResult();
}

async function analyzeResult() {
  try {
    const res = await fetch('/api/analyze-result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        original_src: lastUploadedSrc,
        generated_url: lastGeneratedUrl,
        category: document.getElementById('categorySelect').value,
        template: selectedTemplate,
        current_prompt: lastPrompt,
        current_negative: document.getElementById('negativePrompt').value
      })
    });
    const data = await res.json();
    document.getElementById('feedbackAnalyzing').style.display = 'none';

    if (data.error) { showToast('Analysis failed: ' + data.error, 'error'); return; }

    // Hide the inline feedback panel — we'll use the modal instead
    document.getElementById('feedbackPanel').style.display = 'none';

    // Store for the modal
    window._analysisData = data;

    // Open the interactive correction review modal
    openCorrectionModal(data);

  } catch (e) {
    document.getElementById('feedbackAnalyzing').style.display = 'none';
    showToast('Analysis error: ' + e.message, 'error');
  }
}

// ── CORRECTION REVIEW MODAL ───────────────────────────────
function openCorrectionModal(data) {
  // Images
  document.getElementById('cmOriginal').src = lastUploadedSrc;
  document.getElementById('cmGenerated').src = lastGeneratedUrl + '?t=' + Date.now();

  // Build issue checklist
  const issuesList = document.getElementById('correctionIssuesList');
  issuesList.innerHTML = '';

  // Add duplication warning as first issue if detected
  const allIssues = [...(data.issues || [])];
  if (data.duplication_detected) {
    allIssues.unshift('⚠ Duplication detected: jewellery appears more than once on the model');
  }

  allIssues.forEach((issue, i) => {
    issuesList.appendChild(createIssueCheckItem(issue, true));
  });

  // Prompts
  document.getElementById('cmRefinedPrompt').value = data.refined_prompt || '';
  document.getElementById('cmRefinedNegative').value = data.refined_negative || '';

  // Store duplication flag
  document.getElementById('cmRefinedPrompt').dataset.duplicationDetected = data.duplication_detected ? 'true' : 'false';

  // Show modal
  document.getElementById('correctionOverlay').classList.add('active');
  const modal = document.getElementById('correctionModal');
  modal.style.display = 'block';
  modal.classList.add('active');
}

function createIssueCheckItem(text, checked) {
  const row = document.createElement('div');
  row.className = 'correction-issue-row';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = checked;
  cb.className = 'correction-checkbox';
  const label = document.createElement('span');
  label.className = 'correction-issue-text';
  label.textContent = text;
  const rmBtn = document.createElement('button');
  rmBtn.className = 'correction-issue-rm';
  rmBtn.textContent = '✕';
  rmBtn.title = 'Remove this issue';
  rmBtn.onclick = () => row.remove();
  row.appendChild(cb);
  row.appendChild(label);
  row.appendChild(rmBtn);
  return row;
}

function addCorrectionIssue() {
  const input = document.getElementById('correctionAddInput');
  const text = input.value.trim();
  if (!text) return;
  document.getElementById('correctionIssuesList').appendChild(createIssueCheckItem(text, true));
  input.value = '';
  input.focus();
}

function closeCorrectionModal(e) {
  if (e && e.target !== document.getElementById('correctionOverlay')) return;
  document.getElementById('correctionOverlay').classList.remove('active');
  document.getElementById('correctionModal').style.display = 'none';
  document.getElementById('correctionModal').classList.remove('active');
}

function applyReviewAndRegenerate() {
  // Collect checked issues and build an augmented negative prompt note
  const rows = document.querySelectorAll('#correctionIssuesList .correction-issue-row');
  const confirmedIssues = [];
  rows.forEach(row => {
    const cb = row.querySelector('.correction-checkbox');
    const text = row.querySelector('.correction-issue-text').textContent;
    if (cb.checked) confirmedIssues.push(text);
  });

  const dupFlag = document.getElementById('cmRefinedPrompt').dataset.duplicationDetected === 'true';
  const refinedPos = document.getElementById('cmRefinedPrompt').value;
  const refinedNeg = document.getElementById('cmRefinedNegative').value;

  // Apply to main fields
  if (refinedPos) document.getElementById('customPrompt').value = refinedPos;
  if (refinedNeg) document.getElementById('negativePrompt').value = refinedNeg;
  _duplicationGuardActive = dupFlag;

  closeCorrectionModal();
  showToast(dupFlag
    ? '⚠ Duplication guard active — regenerating...'
    : `✦ Applied ${confirmedIssues.length} correction(s) — regenerating...`, 'success');

  document.getElementById('generateBtn').scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => generateImage(), 400);
}

// Track whether the next generation needs the hard duplication guard
let _duplicationGuardActive = false;

function applyRefinedPrompts() {
  const data = window._analysisData;
  if (!data) return;
  if (data.refined_prompt) document.getElementById('customPrompt').value = data.refined_prompt;
  if (data.refined_negative) document.getElementById('negativePrompt').value = data.refined_negative;
  _duplicationGuardActive = !!data.duplication_detected;
  showToast(_duplicationGuardActive
    ? '⚠ Duplication guard active — click Generate to retry'
    : 'Prompts applied — click Generate to retry', 'success');
}

function applyAndRegenerate() {
  applyRefinedPrompts();
  document.getElementById('generateBtn').scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => generateImage(), 300);
}

// ── GALLERY ───────────────────────────────────────────────
function addToGallery(url, category, template) {
  gallery.unshift({ url, category, template, time: new Date().toLocaleTimeString() });
}

function renderGallery() {
  const grid = document.getElementById('galleryGrid');
  if (gallery.length === 0) {
    grid.innerHTML = `<div class="gallery-empty"><p>Generated images will appear here</p></div>`;
    return;
  }
  grid.innerHTML = gallery.map(g => `
    <div class="gallery-item">
      <img src="${g.url}" alt="${g.category}" />
      <div class="gallery-item-meta">
        <strong>${g.category} — ${g.template}</strong>
        <span>${g.time}</span>
      </div>
    </div>`).join('');
}

function clearGallery() {
  gallery.length = 0;
  renderGallery();
}

// ── MODAL HELPERS ─────────────────────────────────────────
function closeModal(e) {
  if (e.target === document.getElementById('modalOverlay')) closeAllModals();
}
function closeAllModals() {
  document.getElementById('modalOverlay').classList.remove('active');
  document.querySelectorAll('.modal').forEach(m => m.classList.remove('active'));
}

// ── TOAST ─────────────────────────────────────────────────
let toastTimeout;
function showToast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => t.classList.remove('show'), 3000);
}

// Enter key for modals
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeAllModals();
  if (e.key === 'Enter' && document.getElementById('addCategoryModal').classList.contains('active')) {
    addCategory();
  }
});
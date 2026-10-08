const $ = (selector) => document.querySelector(selector);
const elements = {
  form: $('#converter-form'), input: $('#video-url'), field: $('#url-field'), error: $('#form-error'),
  note: $('#service-note'), quality: $('#quality'), primary: $('#convert-button'), preview: $('#video-preview'),
  panel: $('#job-panel'), title: $('#job-title'), description: $('#job-description'), progress: $('#progress-track'),
  fill: $('#progress-fill'), stage: $('#job-stage'), percent: $('#job-percent'), download: $('#download-button'),
  cancel: $('#cancel-button'), restart: $('#restart-button'), expiry: $('#expiry-note'), icon: $('#job-icon'),
};
const apiBase = (window.Y2AUDIO_CONFIG?.apiBase || '').replace(/\/$/, '');
let format = 'mp3', preview = null, job = null, busy = false, timer, expiryTimer, pollFailures = 0, runnerInFlight = false, lastRunAt = 0;
const offlineMessage = 'Conversions aren’t connected in this preview yet. Please come back once the service is live.';

function videoId(value) {
  try {
    const url = new URL(/^[a-z]+:\/\//i.test(value.trim()) ? value.trim() : `https://${value.trim()}`);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.port) return null;
    const host = url.hostname.toLowerCase();
    let id;
    if (host === 'youtu.be') id = url.pathname.slice(1).split('/')[0];
    else if (['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'].includes(host)) {
      if (url.pathname === '/watch') id = url.searchParams.get('v');
      else id = /^\/(?:shorts|embed)\/([\w-]{11})\/?$/.exec(url.pathname)?.[1];
    }
    return /^[\w-]{11}$/.test(id || '') ? id : null;
  } catch { return null; }
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${apiBase}/api${path}`, {
      ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
      signal: AbortSignal.timeout(path === '/preview' ? 50000 : path === '/jobs/run' ? 285000 : 12000), cache: 'no-store',
    });
  } catch (error) {
    throw new Error(error.name === 'TimeoutError' ? 'This is taking longer than expected. Please try again in a moment.' : 'We couldn’t reach the converter. Check your connection and try again.');
  }
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error(offlineMessage);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || data.detail?.message || 'Something went wrong. Please try again.');
    error.status = response.status;
    throw error;
  }
  return data;
}

function showError(message, invalid = false) {
  elements.error.textContent = message;
  elements.error.hidden = !message;
  elements.field.classList.toggle('invalid', invalid);
  elements.input.setAttribute('aria-invalid', String(invalid));
}

function setBusy(value, label) {
  busy = value;
  elements.primary.disabled = value;
  elements.input.disabled = value;
  $('#paste-button').disabled = value;
  $('#clear-video').disabled = value;
  elements.quality.disabled = value;
  document.querySelectorAll('[data-format]').forEach(button => { button.disabled = value; });
  elements.form.setAttribute('aria-busy', String(value));
  elements.primary.querySelector('span').textContent = label || (preview ? `Convert to ${format.toUpperCase()}` : 'Find video');
}

function updateQuality() {
  const previous = elements.quality.value;
  const choices = format === 'mp3'
    ? [['192', '192 kbps · Standard'], ['128', '128 kbps · Smaller file']]
    : [['720', 'Up to 720p · HD'], ['360', 'Up to 360p · Smaller file']];
  elements.quality.replaceChildren(...choices.filter(([value]) => !preview || format === 'mp3' || preview.video.video_qualities.includes(Number(value))).map(([value, label]) => new Option(label, value)));
  if ([...elements.quality.options].some(option => option.value === previous)) elements.quality.value = previous;
  $('label[for="quality"]').textContent = format === 'mp3' ? 'Audio quality' : 'Video quality';
  if (preview && format === 'mp3') {
    const size = preview.video.duration * Number(elements.quality.value) / 8000;
    $('#quality-note').textContent = `Estimated audio size: ${size.toFixed(1)} MB. Output quality depends on the original.`;
  } else $('#quality-note').textContent = format === 'mp3' ? 'MP3 works with virtually any music player.' : 'Available quality depends on the original video.';
  elements.primary.querySelector('span').textContent = preview ? `Convert to ${format.toUpperCase()}` : 'Find video';
}

document.querySelectorAll('[data-format]').forEach(button => button.addEventListener('click', () => {
  if (busy || job) return;
  format = button.dataset.format;
  document.querySelectorAll('[data-format]').forEach(tab => {
    tab.classList.toggle('selected', tab === button);
    tab.setAttribute('aria-pressed', String(tab === button));
  });
  showError('');
  updateQuality();
}));
elements.quality.addEventListener('change', updateQuality);

function clearPreview() {
  preview = null;
  elements.preview.hidden = true;
  updateQuality();
}
elements.input.addEventListener('input', () => { showError(''); clearPreview(); });
$('#clear-video').addEventListener('click', () => { clearPreview(); elements.input.value = ''; elements.input.focus(); });
$('#paste-button').addEventListener('click', async () => {
  try {
    elements.input.value = (await navigator.clipboard.readText()).trim();
    clearPreview(); showError(''); elements.input.focus();
  } catch {
    showError('Paste your link directly into the field using your device’s paste command.');
    elements.input.focus();
  }
});

elements.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  showError('');
  const id = videoId(elements.input.value);
  if (!id) {
    showError('Enter a valid YouTube video link, such as youtube.com/watch?v=… or youtu.be/…', true);
    elements.input.focus(); return;
  }
  if (preview && (format === 'mp4' && !elements.quality.value)) {
    showError('This video doesn’t have a supported MP4 format. Try MP3 audio instead.'); return;
  }
  setBusy(true, preview ? 'Joining the queue…' : 'Finding your video…');
  try {
    if (!preview) {
      preview = await api('/preview', { method: 'POST', body: JSON.stringify({ url: `https://www.youtube.com/watch?v=${id}` }) });
      const video = preview.video;
      $('#video-title').textContent = video.title;
      $('#video-author').textContent = video.author;
      $('#video-duration').textContent = `${Math.floor(video.duration / 60)}:${String(Math.floor(video.duration % 60)).padStart(2, '0')}`;
      $('#video-thumbnail').src = `https://i.ytimg.com/vi/${video.id}/mqdefault.jpg`;
      elements.preview.hidden = false;
      elements.note.hidden = true;
      updateQuality();
      setBusy(false);
      elements.primary.focus();
    } else {
      const result = await api('/jobs', { method: 'POST', body: JSON.stringify({ preview_id: preview.preview_id, format, quality: Number(elements.quality.value) }) });
      job = result;
      try { sessionStorage.setItem('y2audio.job', job.id); } catch { /* Storage may be disabled. */ }
      elements.form.hidden = true;
      elements.panel.hidden = false;
      renderJob(job); pokeRunner(job.id); schedulePoll();
    }
  } catch (error) {
    showError(error.message);
    if (error.status === 410) clearPreview();
    setBusy(false);
  }
});

const jobStates = {
  queued: ['Your file is in the queue', 'We’ll start as soon as a conversion slot opens.', 'Queued'],
  downloading: ['Getting your video', 'Keep this tab open while we prepare your file.', 'Downloading source'],
  processing: ['Making the final touches', 'We’re preparing the file in your chosen format.', 'Processing'],
  completed: ['Your file is ready', 'Save it to your device and you’re all set.', 'Complete'],
  failed: ['We couldn’t finish this one', 'Try again or use another video link.', 'Conversion failed'],
  cancelled: ['Conversion cancelled', 'You can start again with a different video.', 'Cancelled'],
  expired: ['This download has expired', 'The temporary file has been deleted. Convert the video again to get a new link.', 'Expired'],
};
function renderJob(next) {
  job = next;
  format = job.format;
  document.querySelectorAll('[data-format]').forEach(tab => {
    tab.classList.toggle('selected', tab.dataset.format === format);
    tab.setAttribute('aria-pressed', String(tab.dataset.format === format));
    tab.disabled = true;
  });
  const [title, description, stage] = jobStates[job.status] || jobStates.queued;
  const active = ['queued', 'downloading', 'processing'].includes(job.status);
  elements.title.textContent = title;
  elements.description.textContent = job.error || description;
  elements.stage.textContent = job.status === 'queued' && job.position ? `Position ${job.position} in queue` : stage;
  elements.percent.textContent = job.status === 'downloading' && job.progress != null ? `${Math.round(job.progress)}%` : '';
  elements.progress.hidden = !active && job.status !== 'completed';
  const indeterminate = active && (job.status !== 'downloading' || job.progress == null);
  elements.progress.classList.toggle('indeterminate', indeterminate);
  if (indeterminate) elements.progress.removeAttribute('aria-valuenow');
  else elements.progress.setAttribute('aria-valuenow', String(job.status === 'completed' ? 100 : job.progress || 0));
  elements.fill.style.width = indeterminate ? '' : `${job.status === 'completed' ? 100 : job.progress || 0}%`;
  elements.cancel.hidden = !active; elements.cancel.disabled = false;
  elements.cancel.textContent = 'Cancel conversion';
  elements.restart.hidden = active;
  elements.download.hidden = job.status !== 'completed';
  elements.icon.textContent = job.status === 'completed' ? '✓' : active ? '◌' : '·';
  clearInterval(expiryTimer);
  elements.expiry.hidden = job.status !== 'completed';
  if (job.status === 'completed') {
    elements.download.href = `${apiBase}/api/jobs/${encodeURIComponent(job.id)}/download`;
    elements.download.textContent = `Download ${job.format.toUpperCase()} · ${(job.size / 1e6).toFixed(1)} MB`;
    updateExpiry(); expiryTimer = setInterval(updateExpiry, 1000);
  }
  if (!active) { busy = false; clearTimeout(timer); }
}
function updateExpiry() {
  if (!job?.expires_at) return;
  const seconds = Math.max(0, Math.ceil(job.expires_at - Date.now() / 1000));
  elements.expiry.textContent = `Download available for ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}. Your file is automatically deleted afterward.`;
  if (!seconds) renderJob({ ...job, status: 'expired' });
}
function schedulePoll(delay = 1600) { clearTimeout(timer); timer = setTimeout(poll, delay); }
async function pokeRunner(jobId) {
  if (runnerInFlight || Date.now() - lastRunAt < 5000) return;
  runnerInFlight = true; lastRunAt = Date.now();
  try { await api('/jobs/run', { method: 'POST', body: JSON.stringify({ job_id: jobId }) }); }
  catch { /* Status polling will retry the runner if the request could not start. */ }
  finally { runnerInFlight = false; }
}
async function poll() {
  if (!job) return;
  try {
    const next = await api(`/jobs/${encodeURIComponent(job.id)}`);
    pollFailures = 0; renderJob(next);
    if (['queued', 'downloading', 'processing'].includes(next.status)) {
      if (next.status === 'queued' || Date.now() - lastRunAt > 30000) pokeRunner(next.id);
      schedulePoll();
    }
  } catch (error) {
    if ([404, 410].includes(error.status)) { renderJob({ ...job, status: 'expired' }); return; }
    pollFailures += 1;
    elements.description.textContent = 'Connection interrupted. Reconnecting to check your conversion…';
    elements.description.textContent = 'Connection interrupted. Reconnecting to check your conversion…';
    schedulePoll(pollFailures < 12 ? 5000 : 15000);
  }
}
elements.cancel.addEventListener('click', async () => {
  if (!job) return;
  elements.cancel.disabled = true;
  try { renderJob(await api(`/jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' })); }
  catch (error) { elements.description.textContent = error.message; elements.cancel.disabled = false; }
});
elements.restart.addEventListener('click', () => {
  clearTimeout(timer); clearInterval(expiryTimer); job = null; pollFailures = 0;
  try { sessionStorage.removeItem('y2audio.job'); } catch { /* Optional storage. */ }
  elements.panel.hidden = true; elements.form.hidden = false;
  elements.input.value = ''; clearPreview(); showError(''); setBusy(false); elements.input.focus();
});

async function start() {
  try {
    const health = await api('/health');
    if (!health.ready) {
      elements.note.textContent = 'The converter is temporarily unavailable. Please try again shortly.';
      elements.note.hidden = false;
    }
  } catch {
    elements.note.textContent = offlineMessage;
    elements.note.hidden = false;
  }
  let saved;
  try { saved = sessionStorage.getItem('y2audio.job'); } catch { /* Optional storage. */ }
  if (saved && /^[\w-]{20,80}$/.test(saved)) {
    try {
      const existing = await api(`/jobs/${encodeURIComponent(saved)}`);
      elements.form.hidden = true; elements.panel.hidden = false;
      renderJob(existing);
      if (['queued', 'downloading', 'processing'].includes(existing.status)) { pokeRunner(existing.id); schedulePoll(); }
    } catch { try { sessionStorage.removeItem('y2audio.job'); } catch { /* Optional storage. */ } }
  }
}
start();

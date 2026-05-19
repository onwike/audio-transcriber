(() => {
  const $ = (id) => document.getElementById(id);
  const screens = {
    upload: $('screen-upload'),
    progress: $('screen-progress'),
    result: $('screen-result'),
    history: $('screen-history'),
  };
  const dropzone = $('dropzone');
  const fileInput = $('file-input');
  const errorBanner = $('error-banner');
  const errorMessage = $('error-message');

  let currentJobId = null;
  let stagedFile = null;
  let eventSource = null;
  let elapsedTimer = null;
  let elapsedStart = 0;
  let elapsedAccum = 0;  // ms accumulated across pause cycles
  let historyPollTimer = null;
  const HISTORY_POLL_MS = 3000;

  function show(name) {
    Object.values(screens).forEach((s) => s.classList.remove('active'));
    screens[name].classList.add('active');
    // Screen-entry hooks
    if (name === 'upload') refreshHistoryCount();
    if (name === 'history') loadHistory();
    // Stop history polling whenever we leave the history screen
    if (name !== 'history') stopHistoryPolling();
  }

  function showError(msg) {
    errorMessage.textContent = msg;
    errorBanner.classList.remove('hidden');
  }

  function hideError() {
    errorBanner.classList.add('hidden');
  }

  function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtDuration(secs) {
    if (secs == null) return '';
    const s = Math.floor(secs);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${ss}s`;
    return `${ss}s`;
  }

  function fmtPhaseTimings(phaseRuns) {
    if (!phaseRuns) return null;
    const order = ['ingest', 'transcribe', 'polish', 'export'];
    const parts = [];
    let total = 0;
    let countWithDuration = 0;
    for (const phase of order) {
      const run = phaseRuns[phase];
      if (run && typeof run.duration_seconds === 'number') {
        parts.push(`${phase} ${fmtDuration(run.duration_seconds)}`);
        total += run.duration_seconds;
        countWithDuration += 1;
      }
    }
    if (countWithDuration === 0) return null;
    if (countWithDuration > 1) parts.push(`total ${fmtDuration(total)}`);
    return parts.join(' · ');
  }

  function shortenClaudeModel(name) {
    // claude-haiku-4-5-20251001 → "haiku 4.5"
    // claude-sonnet-4-6-20251015 → "sonnet 4.6"
    // claude-opus-4-7            → "opus 4.7"
    const m = name && name.match(/^claude-([a-z]+)-(\d+)-(\d+)(?:-\d+)?$/);
    return m ? `${m[1]} ${m[2]}.${m[3]}` : name;
  }

  function fmtRelative(iso) {
    const date = new Date(iso);
    const diffSec = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diffSec < 60) return 'just now';
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`;
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function fmtElapsed(secs) {
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function resetPhases() {
    document.querySelectorAll('.phases li').forEach((li) =>
      li.classList.remove('active', 'done')
    );
    $('prog-fill').style.width = '0';
  }

  function setPhaseActive(phase) {
    const order = ['ingest', 'transcribe', 'polish', 'export'];
    const idx = order.indexOf(phase);
    if (idx === -1) return;
    document.querySelectorAll('.phases li').forEach((li, i) => {
      li.classList.toggle('done', i < idx);
      li.classList.toggle('active', i === idx);
    });
  }

  function setPhaseAllDone() {
    document.querySelectorAll('.phases li').forEach((li) => {
      li.classList.remove('active');
      li.classList.add('done');
    });
    $('prog-fill').style.width = '100%';
  }

  function _renderElapsed() {
    const totalMs = elapsedAccum + (elapsedStart ? Date.now() - elapsedStart : 0);
    $('prog-elapsed').textContent = fmtElapsed(Math.floor(totalMs / 1000));
  }

  function startElapsedTimer() {
    elapsedStart = Date.now();
    elapsedAccum = 0;
    $('prog-elapsed').textContent = '0:00';
    elapsedTimer = setInterval(_renderElapsed, 500);
  }

  function pauseElapsedTimer() {
    if (elapsedTimer && elapsedStart) {
      elapsedAccum += Date.now() - elapsedStart;
      elapsedStart = 0;
      clearInterval(elapsedTimer);
      elapsedTimer = null;
      _renderElapsed();
    }
  }

  function resumeElapsedTimer() {
    if (!elapsedTimer) {
      elapsedStart = Date.now();
      elapsedTimer = setInterval(_renderElapsed, 500);
    }
  }

  function stopElapsedTimer() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
    elapsedStart = 0;
  }

  // ─── Button visibility ───
  function updateControls(phase, status) {
    const pauseBtn = $('pause-resume-btn');
    const cancelBtn = $('cancel-btn');

    // Cancel: visible whenever a job is in flight (incl. queued — you can
    // cancel a queued job before it ever starts running).
    const inFlight = status === 'running' || status === 'paused' || status === 'queued';
    cancelBtn.classList.toggle('hidden', !inFlight);

    // Pause / Resume: only meaningful during the transcribe phase of a
    // running/paused job. Not for queued (nothing to pause yet) or other phases.
    if (status === 'paused') {
      pauseBtn.textContent = 'Resume';
      pauseBtn.dataset.action = 'resume';
      pauseBtn.classList.remove('hidden');
    } else if (status === 'running' && phase === 'transcribe') {
      pauseBtn.textContent = 'Pause';
      pauseBtn.dataset.action = 'pause';
      pauseBtn.classList.remove('hidden');
    } else {
      pauseBtn.classList.add('hidden');
    }
  }

  function markPhasePaused(phase) {
    document.querySelectorAll('.phases li').forEach((li) =>
      li.classList.remove('active', 'paused')
    );
    const li = document.querySelector(`.phases li[data-phase="${phase}"]`);
    if (li) li.classList.add('paused');
  }

  // ─── Drag & drop (stages only — upload starts when user clicks Start) ───
  ['dragenter', 'dragover'].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.add('dragging');
    })
  );
  ['dragleave', 'drop'].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragging');
    })
  );
  dropzone.addEventListener('drop', (e) => {
    if (e.dataTransfer.files.length) stageFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length) stageFile(e.target.files[0]);
  });

  function stageFile(file) {
    stagedFile = file;
    $('staged-name').textContent = file.name;
    $('staged-meta').textContent = fmtBytes(file.size);
    $('dropzone-empty').classList.add('hidden');
    $('dropzone-staged').classList.remove('hidden');
    dropzone.classList.add('has-file');
    $('start-btn').disabled = false;
  }

  function clearStaged() {
    stagedFile = null;
    fileInput.value = '';
    $('dropzone-empty').classList.remove('hidden');
    $('dropzone-staged').classList.add('hidden');
    dropzone.classList.remove('has-file');
    $('start-btn').disabled = true;
  }

  $('start-btn').addEventListener('click', () => {
    if (!stagedFile) return;
    const file = stagedFile;
    clearStaged();
    upload(file);
  });

  // ─── Clickable logo / title → return to upload screen ───
  $('home-link').addEventListener('click', (e) => {
    e.preventDefault();
    if (eventSource) eventSource.close();
    stopElapsedTimer();
    hideError();
    clearStaged();
    currentJobId = null;
    show('upload');
  });

  // ─── Upload + subscribe ───
  async function upload(file) {
    hideError();
    $('prog-filename').textContent = file.name;
    $('prog-fileinfo').textContent = fmtBytes(file.size);
    resetPhases();
    setPhaseActive('ingest');
    $('prog-message').textContent = 'Uploading…';
    show('progress');
    startElapsedTimer();

    const formData = new FormData();
    formData.append('file', file);
    const selectedModel = document.querySelector('input[name="whisper_model"]:checked');
    if (selectedModel) formData.append('whisper_model', selectedModel.value);

    // Diarization hints (both optional)
    const expectedSpeakers = $('expected-speakers').value.trim();
    if (expectedSpeakers) formData.append('expected_speakers', expectedSpeakers);
    const speakerHintsText = $('speaker-hints').value.trim();
    if (speakerHintsText) formData.append('speaker_hints', speakerHintsText);

    try {
      const res = await fetch('/jobs', { method: 'POST', body: formData });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `Upload failed (${res.status})`);
      }
      const job = await res.json();
      currentJobId = job.id;
      if (job.duration_seconds) {
        $('prog-fileinfo').textContent =
          `${fmtBytes(file.size)} · ${fmtDuration(job.duration_seconds)}`;
      }
      subscribeToJob(job.id);
    } catch (e) {
      stopElapsedTimer();
      show('upload');
      showError(e.message);
    }
  }

  function subscribeToJob(jobId) {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/jobs/${jobId}/events`);
    eventSource.onmessage = (e) => {
      const data = JSON.parse(e.data);

      if (data.status === 'queued') {
        // Job is waiting for the pipeline lock — no active phase yet.
        resetPhases();
        pauseElapsedTimer();
      } else if (data.status === 'paused') {
        markPhasePaused(data.phase);
        pauseElapsedTimer();
      } else if (data.phase) {
        setPhaseActive(data.phase);
      }

      $('prog-fill').style.width = (data.percent || 0) + '%';
      $('prog-message').textContent = data.message;
      updateControls(data.phase, data.status);

      if (data.status === 'running') {
        resumeElapsedTimer();
      } else if (data.status === 'done') {
        eventSource.close();
        setPhaseAllDone();
        stopElapsedTimer();
        updateControls(data.phase, 'done');
        loadResult(jobId);
      } else if (data.status === 'error') {
        eventSource.close();
        stopElapsedTimer();
        updateControls(data.phase, 'error');
        show('upload');
        showError(data.error || data.message);
      } else if (data.status === 'cancelled') {
        eventSource.close();
        stopElapsedTimer();
        updateControls(data.phase, 'cancelled');
        currentJobId = null;
        fileInput.value = '';
        show('upload');
      }
    };
    eventSource.onerror = () => {
      // Transient SSE drops are normal during long phases; let the next event recover.
      // A real failure surfaces via status === 'error' above.
    };
  }

  async function loadResult(jobId) {
    try {
      const res = await fetch(`/jobs/${jobId}/polished`);
      if (!res.ok) throw new Error(`Could not load polished transcript (${res.status})`);
      const polished = await res.json();
      renderTranscript(polished);
      $('download-md').href = `/jobs/${jobId}/download/md`;
      $('download-pdf').href = `/jobs/${jobId}/download/pdf`;
      show('result');
    } catch (e) {
      showError(e.message);
    }
  }

  function renderTranscript(p) {
    const root = $('transcript-preview');
    root.innerHTML = '';

    const h1 = document.createElement('h1');
    h1.textContent = p.title;
    const bq = document.createElement('blockquote');
    // Multi-paragraph summary — split on double newlines and emit a <p> per chunk
    const paragraphs = (p.summary || '').split(/\n{2,}/);
    for (const para of paragraphs) {
      const text = para.trim();
      if (!text) continue;
      const pEl = document.createElement('p');
      pEl.textContent = text;
      bq.append(pEl);
    }
    root.append(h1, bq);

    for (const section of p.sections) {
      const h2 = document.createElement('h2');
      h2.textContent = section.header;
      const stime = document.createElement('p');
      stime.className = 'section-time';
      stime.textContent = section.timestamp;
      root.append(h2, stime);

      for (const para of section.paragraphs) {
        const pEl = document.createElement('p');
        const sp = document.createElement('span');
        sp.className = 'speaker';
        sp.textContent = [para.speaker, para.timestamp].filter(Boolean).join(' · ');
        pEl.append(sp, ' ' + para.text);
        root.append(pEl);
      }
    }
  }

  // ─── History ───
  async function refreshHistoryCount() {
    try {
      const res = await fetch('/jobs');
      if (!res.ok) return;
      const jobs = await res.json();
      const btn = $('view-history-btn');
      if (jobs.length > 0) {
        btn.classList.remove('hidden');
        $('history-count').textContent = jobs.length;
      } else {
        btn.classList.add('hidden');
      }
    } catch (e) {
      // Silent — button just won't show if endpoint unreachable.
    }
  }

  async function loadHistory() {
    try {
      const res = await fetch('/jobs');
      if (!res.ok) throw new Error(`History fetch failed (${res.status})`);
      const jobs = await res.json();
      const list = $('history-list');
      list.innerHTML = '';
      if (jobs.length === 0) {
        $('history-empty').classList.remove('hidden');
      } else {
        $('history-empty').classList.add('hidden');
        for (const job of jobs) list.append(renderHistoryItem(job));
      }

      // Auto-poll while any job is in flight so users don't have to click
      // into each one to see progress. Stops as soon as everything is terminal.
      const anyInFlight = jobs.some((j) =>
        j.status === 'running' || j.status === 'paused' || j.status === 'queued'
      );
      if (anyInFlight) {
        startHistoryPolling();
      } else {
        stopHistoryPolling();
      }
    } catch (e) {
      stopHistoryPolling();
      showError(e.message);
    }
  }

  function startHistoryPolling() {
    if (historyPollTimer) return;  // already running
    historyPollTimer = setInterval(() => {
      // Safety: stop ourselves if the user navigated away between ticks
      if (!screens.history.classList.contains('active')) {
        stopHistoryPolling();
        return;
      }
      loadHistory();  // recursive — will re-decide whether to keep polling
    }, HISTORY_POLL_MS);
  }

  function stopHistoryPolling() {
    if (historyPollTimer) {
      clearInterval(historyPollTimer);
      historyPollTimer = null;
    }
  }

  function renderHistoryItem(job) {
    const li = document.createElement('li');
    li.className = 'history-item';
    li.dataset.jobId = job.id;

    // Header — title + status badge
    const header = document.createElement('div');
    header.className = 'history-item-header';

    const title = document.createElement('h3');
    title.className = 'history-title';
    if (job.title) {
      title.textContent = job.title;
    } else {
      title.classList.add('untitled');
      title.textContent = job.original_filename || '(untitled)';
    }

    const badge = document.createElement('span');
    badge.className = `status-badge ${job.status}`;
    badge.textContent = job.status;

    header.append(title, badge);
    li.append(header);

    // Meta line
    const meta = document.createElement('p');
    meta.className = 'history-meta';
    const bits = [];
    if (job.duration_seconds) bits.push(fmtDuration(job.duration_seconds));
    if (job.whisper_model) bits.push(`whisper: ${job.whisper_model}`);
    if (job.polish_model) bits.push(`polish: ${shortenClaudeModel(job.polish_model)}`);
    if (job.created_at) bits.push(fmtRelative(job.created_at));
    for (const text of bits) {
      const span = document.createElement('span');
      span.textContent = text;
      meta.append(span);
    }
    li.append(meta);

    // Per-phase timings line (only shown when there's data)
    const timings = fmtPhaseTimings(job.phase_runs);
    if (timings) {
      const t = document.createElement('p');
      t.className = 'history-timings';
      t.textContent = timings;
      li.append(t);
    }

    // Live progress line for in-flight jobs (running / paused / queued)
    if (['running', 'paused', 'queued'].includes(job.status) && job.message) {
      const progress = document.createElement('p');
      progress.className = 'history-progress';
      // Add a small percent suffix when relevant and the message doesn't
      // already contain one (transcribe messages embed "X%", but polish
      // and export messages don't).
      const showPct = job.status === 'running'
        && typeof job.percent === 'number'
        && job.percent > 0
        && !/\d%/.test(job.message);
      progress.textContent = showPct
        ? `${job.message} (${job.percent}%)`
        : job.message;
      li.append(progress);
    }

    // Error detail — structured (provider-attributed) takes priority over legacy string
    if (job.status === 'error' && job.error_details) {
      li.append(renderStructuredError(job.error_details));
    } else if (job.status === 'error' && job.error) {
      const err = document.createElement('p');
      err.className = 'history-error-msg';
      err.textContent = job.error;
      li.append(err);
    }

    // Actions
    const actions = document.createElement('div');
    actions.className = 'history-actions';

    if (job.status === 'done') {
      const openBtn = document.createElement('button');
      openBtn.className = 'btn';
      openBtn.type = 'button';
      openBtn.textContent = 'Open';
      openBtn.addEventListener('click', () => openJobFromHistory(job.id));

      const pdf = document.createElement('a');
      pdf.className = 'btn btn-primary';
      pdf.href = `/jobs/${job.id}/download/pdf`;
      pdf.textContent = 'PDF';
      pdf.setAttribute('download', '');

      const md = document.createElement('a');
      md.className = 'btn';
      md.href = `/jobs/${job.id}/download/md`;
      md.textContent = 'MD';
      md.setAttribute('download', '');

      actions.append(openBtn, pdf, md);
    } else if (
      job.status === 'running' ||
      job.status === 'paused' ||
      job.status === 'queued'
    ) {
      const watchBtn = document.createElement('button');
      watchBtn.className = 'btn btn-primary';
      watchBtn.type = 'button';
      watchBtn.textContent = 'Watch progress';
      watchBtn.addEventListener('click', () => watchJob(job.id));
      actions.append(watchBtn);
    } else if (job.status === 'error') {
      // Failed jobs whose Whisper transcript made it to disk can be polished
      // from history without re-uploading audio. The transcript is saved as
      // soon as phase advances past TRANSCRIBE, so phase=polish or phase=export
      // at time of error means transcript.json exists.
      const transcriptExists = job.phase === 'polish' || job.phase === 'export';
      if (transcriptExists) {
        const polishBtn = document.createElement('button');
        polishBtn.className = 'btn btn-primary';
        polishBtn.type = 'button';
        polishBtn.textContent = 'Polish';
        polishBtn.title = 'Retry the polish step using the saved transcript (no Whisper rerun)';
        polishBtn.addEventListener('click', () =>
          polishFromHistory(job.id, job.title || job.original_filename || '(re-polishing)')
        );
        actions.append(polishBtn);
      }
    }

    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    actions.append(spacer);

    const del = document.createElement('button');
    del.className = 'btn btn-text';
    del.type = 'button';
    del.textContent = 'Delete';
    del.addEventListener('click', () => deleteJobFromHistory(job.id, job.title || job.original_filename));
    actions.append(del);

    li.append(actions);
    return li;
  }

  function renderStructuredError(err) {
    // err = { source, title, detail, links: [{label, url}], raw }
    const box = document.createElement('div');
    box.className = 'history-error-box';

    const header = document.createElement('div');
    header.className = 'history-error-header';

    const sourceBadge = document.createElement('span');
    sourceBadge.className = `error-source-badge error-source-${err.source || 'internal'}`;
    sourceBadge.textContent = (err.source || 'internal').toUpperCase();
    header.append(sourceBadge);

    const title = document.createElement('strong');
    title.className = 'history-error-title';
    title.textContent = err.title || 'Error';
    header.append(title);

    box.append(header);

    if (err.detail) {
      const detail = document.createElement('p');
      detail.className = 'history-error-detail';
      detail.textContent = err.detail;
      box.append(detail);
    }

    if (err.links && err.links.length) {
      const linksRow = document.createElement('div');
      linksRow.className = 'history-error-links';
      for (const link of err.links) {
        const a = document.createElement('a');
        a.href = link.url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = link.label;
        a.className = 'history-error-link';
        linksRow.append(a);
      }
      box.append(linksRow);
    }

    if (err.raw) {
      const details = document.createElement('details');
      details.className = 'history-error-raw';
      const summary = document.createElement('summary');
      summary.textContent = 'Raw technical detail';
      details.append(summary);
      const pre = document.createElement('pre');
      pre.textContent = err.raw;
      details.append(pre);
      box.append(details);
    }

    return box;
  }

  async function openJobFromHistory(jobId) {
    currentJobId = jobId;
    await loadResult(jobId);
  }

  async function polishFromHistory(jobId, label) {
    // Kick off re-polish from a saved transcript and jump to the progress
    // screen so the user can watch it run.
    hideError();
    currentJobId = jobId;
    $('prog-filename').textContent = label;
    $('prog-fileinfo').textContent = '(re-polishing existing transcript)';
    resetPhases();
    setPhaseActive('polish');
    $('prog-message').textContent = 'Re-polishing…';
    show('progress');
    stopElapsedTimer();
    startElapsedTimer();
    try {
      const res = await fetch(`/jobs/${jobId}/repolish`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `Re-polish failed (${res.status})`);
      }
      subscribeToJob(jobId);
    } catch (e) {
      stopElapsedTimer();
      show('history');  // back to history so the user can retry / delete
      showError(e.message);
    }
  }

  async function watchJob(jobId) {
    // Reattach the progress UI to an in-flight (running or paused) job.
    // The SSE bus replays every past event on subscribe so the phase
    // indicators / percent / message catch up automatically; we just
    // need to set the filename, reset the elapsed timer to "rejoined now",
    // and let the live stream drive everything else.
    try {
      const res = await fetch(`/jobs/${jobId}`);
      if (!res.ok) throw new Error(`Could not load job (${res.status})`);
      const job = await res.json();

      hideError();
      currentJobId = jobId;
      $('prog-filename').textContent = job.original_filename || '(audio)';
      $('prog-fileinfo').textContent =
        job.duration_seconds ? fmtDuration(job.duration_seconds) : '';
      resetPhases();
      $('prog-message').textContent = 'Reattaching to live stream…';
      stopElapsedTimer();
      startElapsedTimer();

      show('progress');
      // Subscribe — replay catches the UI up to current state, then live events take over.
      subscribeToJob(jobId);
    } catch (e) {
      showError(e.message);
    }
  }

  async function deleteJobFromHistory(jobId, label) {
    if (!confirm(`Delete "${label}"?\n\nThis removes the PDF, Markdown, and all working files. This cannot be undone.`)) return;
    try {
      const res = await fetch(`/jobs/${jobId}`, { method: 'DELETE' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `Delete failed (${res.status})`);
      }
      if (currentJobId === jobId) currentJobId = null;
      await loadHistory();
    } catch (e) {
      showError(e.message);
    }
  }

  // ─── Navigation buttons ───
  $('view-history-btn').addEventListener('click', () => show('history'));
  $('view-history-from-result-btn').addEventListener('click', () => show('history'));
  $('new-from-history-btn').addEventListener('click', () => {
    hideError();
    clearStaged();
    currentJobId = null;
    show('upload');
  });

  // ─── Buttons ───
  $('restart-btn').addEventListener('click', () => {
    if (eventSource) eventSource.close();
    stopElapsedTimer();
    hideError();
    clearStaged();
    currentJobId = null;
    show('upload');
  });

  $('error-dismiss').addEventListener('click', () => {
    hideError();
    show('upload');
  });

  // ─── Pause / Resume / Stop ───
  $('pause-resume-btn').addEventListener('click', async () => {
    if (!currentJobId) return;
    const action = $('pause-resume-btn').dataset.action || 'pause';
    try {
      const res = await fetch(`/jobs/${currentJobId}/${action}`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `${action} failed (${res.status})`);
      }
      // The SSE channel will deliver the resulting 'paused' / 'running' event;
      // UI updates happen there so they stay in sync with server truth.
    } catch (e) {
      showError(e.message);
    }
  });

  $('cancel-btn').addEventListener('click', async () => {
    if (!currentJobId) return;
    $('prog-message').textContent = 'Stopping…';
    try {
      const res = await fetch(`/jobs/${currentJobId}/cancel`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `Cancel failed (${res.status})`);
      }
      // The 'cancelled' SSE event drops us back to the upload screen.
    } catch (e) {
      showError(e.message);
    }
  });

  // ─── Initial state: populate history count on first load ───
  refreshHistoryCount();

  $('repolish-btn').addEventListener('click', async () => {
    if (!currentJobId) return;
    hideError();
    resetPhases();
    setPhaseActive('polish');
    $('prog-message').textContent = 'Re-polishing…';
    show('progress');
    startElapsedTimer();
    try {
      const res = await fetch(`/jobs/${currentJobId}/repolish`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `Re-polish failed (${res.status})`);
      }
      subscribeToJob(currentJobId);
    } catch (e) {
      stopElapsedTimer();
      show('upload');
      showError(e.message);
    }
  });
})();

(() => {
  const $ = (id) => document.getElementById(id);
  const screens = {
    upload: $('screen-upload'),
    progress: $('screen-progress'),
    result: $('screen-result'),
  };
  const dropzone = $('dropzone');
  const fileInput = $('file-input');
  const errorBanner = $('error-banner');
  const errorMessage = $('error-message');

  let currentJobId = null;
  let eventSource = null;
  let elapsedTimer = null;
  let elapsedStart = 0;

  function show(name) {
    Object.values(screens).forEach((s) => s.classList.remove('active'));
    screens[name].classList.add('active');
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

  function startElapsedTimer() {
    elapsedStart = Date.now();
    $('prog-elapsed').textContent = '0:00';
    elapsedTimer = setInterval(() => {
      $('prog-elapsed').textContent = fmtElapsed(
        Math.floor((Date.now() - elapsedStart) / 1000)
      );
    }, 500);
  }

  function stopElapsedTimer() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  // ─── Drag & drop ───
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
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length) upload(e.target.files[0]);
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
      if (data.phase) setPhaseActive(data.phase);
      $('prog-fill').style.width = data.percent + '%';
      $('prog-message').textContent = data.message;
      if (data.status === 'done') {
        eventSource.close();
        setPhaseAllDone();
        stopElapsedTimer();
        loadResult(jobId);
      } else if (data.status === 'error') {
        eventSource.close();
        stopElapsedTimer();
        show('upload');
        showError(data.error || data.message);
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
    bq.textContent = p.summary;
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

  // ─── Buttons ───
  $('restart-btn').addEventListener('click', () => {
    if (eventSource) eventSource.close();
    stopElapsedTimer();
    hideError();
    fileInput.value = '';
    currentJobId = null;
    show('upload');
  });

  $('error-dismiss').addEventListener('click', () => {
    hideError();
    show('upload');
  });

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

// MediClear voice features:
//   1. A microphone recorder (used on the consultation page and the symptom check page)
//   2. The patient symptom check page
// Recording works on http://localhost. On a website it needs https:// (browsers block the microphone otherwise).

// ---------------------------------------------------------------------------
// 1. Microphone recorder
// ---------------------------------------------------------------------------
function pickMimeType() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  return options.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
}

function micErrorMessage(e) {
  if (!window.isSecureContext) return "The microphone only works on http://localhost or an https:// address.";
  if (e?.name === "NotAllowedError") return "Microphone permission was blocked. Click the lock icon in the address bar, allow the microphone, and try again.";
  if (e?.name === "NotFoundError") return "No microphone was found. Plug one in or check your sound settings.";
  if (e?.name === "NotReadableError") return "The microphone is being used by another app. Close it and try again.";
  return `Couldn't start the microphone: ${e?.message || e}`;
}

function fmtTime(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function newSessionId() {
  return window.crypto?.randomUUID ? crypto.randomUUID() : `s-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

// Microphone audio arrives at 44.1 or 48 kHz; Whisper wants 16 kHz, 16-bit numbers.
function downsampleTo16k(input, inRate) {
  const ratio = inRate / 16000;
  const out = new Int16Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const a = Math.floor(i * ratio), b = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = a; j < b; j++) sum += input[j];
    const v = Math.max(-1, Math.min(1, sum / Math.max(1, b - a)));
    out[i] = v * 0x7fff;
  }
  return out;
}

/**
 * Turns a .recorder block into a working recorder with live subtitles.
 * onChange(file, subtitles) is called when a recording is ready (subtitles = {text, language} or null),
 * and onChange(null) when it is discarded.
 */
function createRecorder(rootId, { maxSeconds, onChange, onError, liveEveryMs = 3000 }) {
  const root = $(rootId), ready = $(`${rootId}-ready`), caps = $(`${rootId}-captions`);
  const q = (role, el = root) => el.querySelector(`[data-role="${role}"]`);
  const btn = q("toggle"), label = q("label"), hint = q("hint"), level = q("level"), time = q("time");
  const audio = q("audio", ready), discard = q("discard", ready);
  const capText = q("captext", caps), capLang = q("lang", caps);
  const idle = { label: label.textContent, hint: hint.textContent };
  let rec = null, stream = null, chunks = [], started = 0, timer = null, raf = null, ctx = null, file = null, audioUrl = null;
  // live subtitles
  let sid = null, pending = [], liveTimer = null, inflight = null, liveResult = null, liveBroken = false, processor = null;

  function showCaptions(r, { finishing = false } = {}) {
    caps.classList.remove("hidden");
    if (!r || (!r.text && !r.tentative)) {
      capText.innerHTML = `<span class="cap-wait">${finishing ? "Finishing…" : "Listening… start speaking. (The very first time, the speech model can take a minute to load.)"}</span>`;
    } else {
      capText.innerHTML = r.committed.map((l) => `<span>${esc(l)} </span>`).join("") +
        (r.tentative ? `<span class="tent">${esc(r.tentative)}</span>` : "");
    }
    capLang.textContent = r?.language_name || "";
    capText.scrollTop = capText.scrollHeight;
  }

  function takePending() {
    const total = pending.reduce((n, a) => n + a.length, 0);
    const out = new Int16Array(total);
    let o = 0;
    for (const a of pending) { out.set(a, o); o += a.length; }
    pending = [];
    return out;
  }

  async function sendLive(final) {
    if (liveBroken || !sid) return null;
    if (inflight) {
      if (!final) return null;      // still busy with the last update: skip, the next one catches up
      await inflight.catch(() => {});
    }
    const body = takePending();
    const job = fetch(`/api/live/${sid}?final=${final ? 1 : 0}`, {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: body.buffer,
    }).then(async (res) => {
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error(data?.detail || `Subtitles failed (${res.status})`);
      return data;
    });
    inflight = job;
    try {
      const r = await job;
      liveResult = r;
      showCaptions(r);
      return r;
    } catch (e) {
      liveBroken = true;
      caps.classList.remove("hidden");
      capText.innerHTML = `<span class="cap-wait">Live subtitles aren't available (${esc(e.message)}). The recording still works.</span>`;
      return null;
    } finally {
      if (inflight === job) inflight = null;
    }
  }

  async function start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      onError?.("This browser can't record audio. Use a recent Chrome, Edge or Firefox.");
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (e) {
      onError?.(micErrorMessage(e));
      return;
    }
    const mimeType = pickMimeType();
    rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunks = [];
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = finish;
    rec.start(1000);
    started = Date.now();
    root.classList.add("recording");
    btn.setAttribute("aria-label", "Stop recording");
    label.textContent = "Recording… click to stop";
    hint.textContent = "Speak normally. Your words appear below as you talk.";
    time.textContent = "00:00";
    ready.classList.add("hidden");
    timer = setInterval(() => {
      const sec = (Date.now() - started) / 1000;
      time.textContent = fmtTime(sec);
      if (sec >= maxSeconds) stop();
    }, 250);
    // live subtitles
    sid = newSessionId(); pending = []; liveResult = null; liveBroken = false;
    showCaptions(null);
    startAudioGraph(stream);
    liveTimer = setInterval(() => sendLive(false), liveEveryMs);
  }

  function startAudioGraph(s) {
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const source = ctx.createMediaStreamSource(s);
      // level bar
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      const draw = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += ((v - 128) / 128) ** 2;
        level.style.width = `${Math.min(100, Math.sqrt(sum / data.length) * 400)}%`;
        raf = requestAnimationFrame(draw);
      };
      draw();
      // raw audio for the live subtitles
      processor = ctx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (e) => pending.push(downsampleTo16k(e.inputBuffer.getChannelData(0), ctx.sampleRate));
      const mute = ctx.createGain();
      mute.gain.value = 0;  // keeps the audio flowing without playing it through the speakers
      source.connect(processor); processor.connect(mute); mute.connect(ctx.destination);
    } catch {
      liveBroken = true;  // the level bar and subtitles are optional; the recording itself still works
    }
  }

  function stopAudioGraph() {
    clearInterval(timer); clearInterval(liveTimer); cancelAnimationFrame(raf);
    if (processor) processor.onaudioprocess = null;
    ctx?.close(); stream?.getTracks().forEach((t) => t.stop());
    ctx = null; processor = null;
  }

  function stop() {
    if (rec && rec.state !== "inactive") rec.stop();
  }

  async function finish() {
    const seconds = (Date.now() - started) / 1000;
    stopAudioGraph();
    root.classList.remove("recording");
    btn.setAttribute("aria-label", "Start recording");
    level.style.width = "0";
    const type = rec.mimeType || "audio/webm";
    const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
    const blob = new Blob(chunks, { type });
    if (seconds < 1 || blob.size < 1000) {
      reset();
      onError?.("That recording was too short. Hold on a moment before clicking stop.");
      return;
    }
    file = new File([blob], `recording-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${ext}`, { type });
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(blob);
    audio.src = audioUrl;
    time.textContent = "";
    btn.disabled = true;
    label.textContent = "Finishing the subtitles…";
    hint.textContent = "Just a moment.";
    if (!liveResult?.text) showCaptions(null, { finishing: true });
    const final = await sendLive(true);
    sid = null;
    btn.disabled = false;
    label.textContent = `Recording ready (${fmtTime(seconds)})`;
    hint.textContent = "Listen to it below, record again, or continue.";
    ready.classList.remove("hidden");
    onChange?.(file, final && final.text ? { text: final.text, language: final.language } : null);
  }

  function reset() {
    if (rec && rec.state !== "inactive") { rec.onstop = null; rec.stop(); }
    stopAudioGraph();
    if (sid) fetch(`/api/live/${sid}`, { method: "DELETE" }).catch(() => {});
    sid = null; pending = []; liveResult = null;
    root.classList.remove("recording");
    btn.disabled = false;
    file = null;
    label.textContent = idle.label; hint.textContent = idle.hint; time.textContent = "";
    level.style.width = "0";
    audio.removeAttribute("src");
    ready.classList.add("hidden");
    caps.classList.add("hidden");
    capText.innerHTML = ""; capLang.textContent = "";
    onChange?.(null, null);
  }

  btn.addEventListener("click", () => (rec && rec.state === "recording" ? stop() : start()));
  discard.addEventListener("click", reset);
  return { getFile: () => file, getSubtitles: () => (file && liveResult?.text ? liveResult : null), reset };
}

// Recorder on the consultation page
const consultRecorder = createRecorder("rec-consult", {
  maxSeconds: 45 * 60,
  onChange: (f, subs) => {
    if (!f) return;
    $("file").value = "";
    $("drop-text").innerHTML = '<b>Choose a recording or video</b> or drag it here<br><small>mp3, wav, m4a, mp4, mov, webm… or a .txt transcript</small>';
    // English subtitles become the transcript, so processing can skip transcribing again
    if (subs?.language === "en") $("transcript").value = subs.text;
  },
  onError: (msg) => { $("new-error").textContent = msg; $("new-error").classList.remove("hidden"); },
});
window.getConsultRecording = () => consultRecorder.getFile();
window.getConsultLive = () => consultRecorder.getSubtitles();
window.resetConsultRecorder = () => consultRecorder.reset();
$("file").addEventListener("change", () => { if ($("file").files[0] && consultRecorder.getFile()) consultRecorder.reset(); });

// ---------------------------------------------------------------------------
// 2. Symptom check page
// ---------------------------------------------------------------------------
let patientSamples = [];
const checkRecorder = createRecorder("rec-check", {
  maxSeconds: 3 * 60,
  onChange: (f, subs) => {
    if (!f) return;
    $("check-file").value = ""; $("check-file-name").textContent = "";
    if (subs?.text) $("check-text").value = subs.text.replace(/\n/g, " ");
    if ($("check-auto").checked) $("form-check").requestSubmit();  // answer straight away
  },
  onError: showCheckError,
});

// Remember the two options (per browser)
for (const id of ["check-auto", "check-speak"]) {
  try { const v = localStorage.getItem(`mediclear-${id}`); if (v !== null) $(id).checked = v === "1"; } catch { /* storage blocked */ }
  $(id).addEventListener("change", () => { try { localStorage.setItem(`mediclear-${id}`, $(id).checked ? "1" : "0"); } catch { /* ignore */ } });
}

function showCheckError(msg) {
  $("check-error").textContent = msg;
  $("check-error").classList.remove("hidden");
}

function showCheck() {
  current = null;
  $("view-new").classList.add("hidden");
  $("view-result").classList.add("hidden");
  $("view-check").classList.remove("hidden");
  $("form-check").classList.remove("hidden");
  $("check-result").classList.add("hidden");
  $("check-error").classList.add("hidden");
  setNav("check");
  document.querySelectorAll("#history li").forEach((li) => li.classList.remove("active"));
}

async function loadPatientSamples() {
  try { patientSamples = await api("/api/samples/patient"); } catch { patientSamples = []; }
  $("check-sample").innerHTML = '<option value="">Try an example…</option>' +
    patientSamples.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join("");
}

$("btn-check").addEventListener("click", showCheck);
$("check-sample").addEventListener("change", (e) => { if (e.target.value !== "") $("check-text").value = patientSamples[e.target.value].text; });
$("check-file").addEventListener("change", () => {
  const f = $("check-file").files[0];
  $("check-file-name").textContent = f ? `${f.name} selected` : "";
  if (f && checkRecorder.getFile()) checkRecorder.reset();
});

$("form-check").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("check-error").classList.add("hidden");
  const fd = new FormData();
  const recording = checkRecorder.getFile();
  const subs = checkRecorder.getSubtitles();
  const text = $("check-text").value.trim();
  // English recording with subtitles: the subtitles already are the text, so skip transcribing again (faster).
  // Other languages: send the recording, so it's translated to English properly.
  const useSubtitles = recording && subs?.language === "en" && text;
  const f = useSubtitles ? null : (recording || $("check-file").files[0]);
  if (!f && !text) { showCheckError("Record your voice or type your symptoms first."); return; }
  if (f) fd.append("file", f);
  fd.append("text", text);
  if (useSubtitles) fd.append("language", "en");
  stopSpeaking();
  $("form-check").classList.add("hidden");
  $("check-processing").classList.remove("hidden");
  try {
    renderCheck(await api("/api/symptom-check", { method: "POST", body: fd }));
  } catch (ex) {
    $("form-check").classList.remove("hidden");
    showCheckError(ex.message);
  } finally {
    $("check-processing").classList.add("hidden");
  }
});

const URGENCY_ICON = { emergency: "!", urgent_today: "!", see_doctor_soon: "i", self_care: "✓" };

function renderCheck(r) {
  const box = $("check-result");
  const tr = r.transcript || {};
  const lang = tr.language_name && tr.language !== "en"
    ? `<p class="muted">You spoke ${esc(tr.language_name)}${tr.translated ? "; translated to English below" : ""}.</p>` : "";
  box.innerHTML = `
    <div class="urgency u-${r.urgency}">
      <span class="u-icon">${URGENCY_ICON[r.urgency]}</span>
      <div><div class="u-level">${esc(r.urgency_text)}</div>
        ${r.urgency_reason ? `<div>${esc(r.urgency_reason)}</div>` : ""}</div>
    </div>

    ${r.red_flags.length ? `<div class="card redflags"><h2>Danger signs in what you said</h2><ul class="plain">
      ${r.red_flags.map((f) => `<li><b>${esc(f.name)}:</b> ${esc(f.message)} <small class="muted">(you said “${esc(f.matched)}”)</small></li>`).join("")}
      </ul></div>` : ""}

    ${(r.warnings || []).map((w) => `<div class="warning">⚠ ${esc(w)}</div>`).join("")}

    <div class="grid">
      <div class="card"><h2>What it might be</h2>
        ${r.possible_issues.length ? r.possible_issues.map((i) => `
          <div class="issue">
            <div class="issue-head"><b>${esc(i.simple_name || i.name)}</b>
              <span class="chip like-${i.likelihood.replace(" ", "-")}">${esc(i.likelihood)}</span></div>
            <small class="muted">${esc(i.name)}${i.icd10 ? ` · ICD-10 ${esc(i.icd10)}` : ""}</small>
            ${i.why ? `<p>${esc(i.why)}</p>` : ""}
          </div>`).join("") : '<p class="empty">No suggestions. Please describe your symptoms in more detail.</p>'}
        <p class="muted small">Only a doctor can tell you what it really is.</p>
      </div>

      <div class="card"><h2>What you told us</h2>
        ${r.symptoms.length ? `<table><tr><th>Symptom</th><th>For</th><th>How bad</th></tr>
          ${r.symptoms.map((s) => `<tr><td>${esc(s.name)}${s.patient_words ? `<br><small class="muted">“${esc(s.patient_words)}”</small>` : ""}</td>
            <td>${esc(s.duration)}</td><td>${s.severity && s.severity !== "unknown" ? esc(s.severity) : ""}</td></tr>`).join("")}</table>` : '<p class="empty">No symptoms found</p>'}
        <details><summary>What we heard</summary>${lang}<p>${esc(tr.text)}</p></details>
      </div>

      <div class="card"><h2>What you can do now</h2>${list(r.self_care)}
        <h3>Get help sooner if</h3>${list(r.see_doctor_if)}</div>

      <div class="card"><h2>Who to see</h2><p>${esc(r.doctor_type) || '<span class="empty">A general doctor</span>'}</p>
        <h3>The doctor may ask you</h3>${list(r.questions_doctor_may_ask)}
        ${r.missing_info.length ? `<h3>Also tell the doctor</h3>${list(r.missing_info)}` : ""}</div>
    </div>

    <div class="notice">This is not a diagnosis and doesn't replace a doctor. If you feel very unwell or something gets worse,
      get medical help straight away.</div>

    <div class="actions">
      <button class="btn" id="btn-speak"><span aria-hidden="true">🔊</span> Read aloud</button>
      <button class="btn" id="btn-check-again">Check again</button>
      <button class="btn primary" id="btn-check-to-consult">Start a consultation with this</button>
    </div>`;
  box.classList.remove("hidden");
  box.scrollIntoView({ behavior: "smooth", block: "start" });

  $("btn-speak").addEventListener("click", () => (speaking() ? stopSpeaking() : speakResult(r)));
  if ($("check-speak").checked) speakResult(r);

  $("btn-check-again").addEventListener("click", () => {
    stopSpeaking();
    checkRecorder.reset();
    $("check-text").value = ""; $("check-file").value = ""; $("check-file-name").textContent = "";
    showCheck();
  });
  $("btn-check-to-consult").addEventListener("click", () => {
    stopSpeaking();
    showNew();
    $("transcript").value = `Patient: ${tr.text}`;
    $("transcript").focus();
  });
}

// ---------------------------------------------------------------------------
// 3. Read the answer out loud (uses the voices built into the computer, works offline)
// ---------------------------------------------------------------------------
function speaking() { return !!window.speechSynthesis?.speaking; }

function stopSpeaking() {
  window.speechSynthesis?.cancel();
  const b = $("btn-speak");
  if (b) b.innerHTML = '<span aria-hidden="true">🔊</span> Read aloud';
}

function resultToSpeech(r) {
  const parts = [r.urgency_text + "."];
  if (r.urgency_reason) parts.push(r.urgency_reason);
  for (const f of r.red_flags) parts.push(f.message);
  if (r.possible_issues.length) {
    const names = r.possible_issues.map((i) => i.simple_name || i.name);
    parts.push(names.length === 1 ? `It might be ${names[0]}.`
      : `It might be ${names.slice(0, -1).join(", ")}, or ${names[names.length - 1]}.`);
  }
  if (r.self_care.length) parts.push("What you can do now: " + r.self_care.join(". ") + ".");
  if (r.see_doctor_if.length) parts.push("Get help sooner if: " + r.see_doctor_if.join(". ") + ".");
  if (r.doctor_type) parts.push(`You can see a ${r.doctor_type}.`);
  parts.push("Remember, this is not a diagnosis. Only a doctor can tell you what it really is.");
  return parts.join(" ").replace(/\.\./g, ".");
}

function speakResult(r) {
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return;
  stopSpeaking();
  const u = new SpeechSynthesisUtterance(resultToSpeech(r));
  u.lang = "en-US";
  u.rate = 0.95;
  const voice = speechSynthesis.getVoices().find((v) => v.lang?.startsWith("en"));
  if (voice) u.voice = voice;
  u.onend = u.onerror = () => stopSpeaking();
  speechSynthesis.speak(u);
  const b = $("btn-speak");
  if (b) b.innerHTML = '<span aria-hidden="true">⏹</span> Stop reading';
}

loadPatientSamples();

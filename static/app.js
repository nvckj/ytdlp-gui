const $ = (id) => document.getElementById(id);
const urlInput = $("url"), fetchBtn = $("fetchBtn"), fetchStatus = $("fetchStatus");
const result = $("result"), queueSection = $("queueSection"), queueEl = $("queue");

let currentInfo = null;
let mode = "video"; // "video" | "audio"
const jobs = new Map(); // job_id -> {el refs, timer}

function fmtDur(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}
function fmtSpeed(bps) {
  if (!bps) return "";
  return bps > 1048576 ? (bps / 1048576).toFixed(1) + " MB/s" : Math.round(bps / 1024) + " KB/s";
}
function fmtEta(sec) {
  if (sec == null) return "";
  return sec < 60 ? sec + "s left" : Math.floor(sec / 60) + "m " + (sec % 60) + "s left";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

fetchBtn.addEventListener("click", doFetch);
urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doFetch(); });

async function doFetch() {
  const url = urlInput.value.trim();
  if (!url) return;
  fetchBtn.disabled = true;
  fetchStatus.className = "status";
  fetchStatus.innerHTML = '<span class="spin"></span>Reading link…';
  result.classList.add("hidden");
  try {
    const r = await fetch("/api/info", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Fetch failed");
    currentInfo = data;
    fetchStatus.textContent = "";
    renderResult(data);
  } catch (e) {
    fetchStatus.className = "status error";
    fetchStatus.textContent = "Couldn't read that link: " + e.message;
  } finally {
    fetchBtn.disabled = false;
  }
}

function optionsHTML(formats) {
  let qualityOpts;
  if (formats && formats.length) {
    qualityOpts = `<option value="best">Best available</option>` + formats.map((f) => {
      const size = f.filesize ? " · ~" + Math.round(f.filesize / 1048576) + " MB" : "";
      const fps = f.fps && f.fps > 30 ? f.fps + "fps" : "";
      return `<option value="${f.height}">${f.height}p ${esc(fps)}${esc(size)}</option>`;
    }).join("");
  } else {
    qualityOpts = ["best", "2160", "1440", "1080", "720", "480", "360"].map((q) =>
      `<option value="${q}">${q === "best" ? "Best available" : q + "p"}</option>`).join("");
  }
  return `
    <div class="options">
      <div class="seg" id="modeSeg">
        <button data-mode="video" class="active">🎬 Video</button>
        <button data-mode="audio">🎵 MP3</button>
      </div>
      <select id="quality">${qualityOpts}</select>
    </div>
    <p class="hint" id="modeHint">Video is merged into MP4 <b>without re-encoding</b> — original quality, fast.</p>`;
}

function renderResult(data) {
  mode = "video";
  if (data.type === "video") {
    result.innerHTML = `
      <div class="video-card">
        ${data.thumbnail ? `<img class="thumb" src="${esc(data.thumbnail)}" alt="">` : ""}
        <div class="video-meta">
          <h3>${esc(data.title)}</h3>
          <div class="meta">${esc(data.uploader || "")}${data.duration ? " · " + fmtDur(data.duration) : ""}</div>
        </div>
      </div>
      ${optionsHTML(data.formats)}
      <div class="download-row"><button id="dlBtn" class="primary">Download</button></div>`;
    $("dlBtn").addEventListener("click", () => {
      startDownload(data.webpage_url, data.title);
    });
  } else {
    const items = data.entries.map((e, i) => `
      <label class="pl-item">
        <input type="checkbox" data-i="${i}" checked>
        ${e.thumbnail ? `<img src="${esc(e.thumbnail)}" alt="" loading="lazy">` : ""}
        <span class="t">${esc(e.title)}</span>
        <span class="d">${fmtDur(e.duration)}</span>
      </label>`).join("");
    result.innerHTML = `
      <div class="pl-head"><h3>${esc(data.title)}</h3><span class="count">${data.count} videos</span></div>
      ${optionsHTML(null)}
      <div class="pl-actions">
        <button class="ghost" id="selAll">Select all</button>
        <button class="ghost" id="selNone">Select none</button>
      </div>
      <div class="pl-list">${items}</div>
      <div class="download-row"><button id="dlBtn" class="primary">Download selected</button></div>`;
    $("selAll").addEventListener("click", () => result.querySelectorAll('.pl-item input').forEach((c) => c.checked = true));
    $("selNone").addEventListener("click", () => result.querySelectorAll('.pl-item input').forEach((c) => c.checked = false));
    $("dlBtn").addEventListener("click", () => {
      const checked = [...result.querySelectorAll('.pl-item input:checked')];
      if (!checked.length) return;
      checked.forEach((c) => {
        const e = data.entries[+c.dataset.i];
        if (e && e.url) startDownload(e.url, e.title);
      });
    });
  }
  result.classList.remove("hidden");
  // mode toggle
  const seg = $("modeSeg");
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    mode = b.dataset.mode;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    $("quality").style.display = mode === "video" ? "" : "none";
    $("modeHint").innerHTML = mode === "video"
      ? "Video is merged into MP4 <b>without re-encoding</b> — original quality, fast."
      : "Audio is extracted to 192 kbps MP3 (requires ffmpeg on the server).";
  });
  result.scrollIntoView({behavior: "smooth", block: "nearest"});
}

async function startDownload(url, title) {
  const quality = mode === "video" ? ($("quality") ? $("quality").value : "best") : "best";
  queueSection.classList.remove("hidden");
  const el = document.createElement("div");
  el.className = "job";
  el.innerHTML = `
    <div class="jt">${esc(title || url)}</div>
    <div class="bar"><div></div></div>
    <div class="jr"><span class="st">Starting…</span><span class="badge downloading">QUEUED</span></div>`;
  queueEl.prepend(el);
  const refs = { bar: el.querySelector(".bar > div"), st: el.querySelector(".st"),
                 badge: el.querySelector(".badge"), root: el };
  try {
    const r = await fetch("/api/download", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url, mode, quality}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Download failed to start");
    jobs.set(data.job_id, refs);
    pollJob(data.job_id, refs);
  } catch (e) {
    refs.badge.className = "badge error";
    refs.badge.textContent = "ERROR";
    refs.st.textContent = "";
    const err = document.createElement("div");
    err.className = "err"; err.textContent = e.message;
    el.appendChild(err);
  }
}

async function pollJob(jobId, refs) {
  try {
    const r = await fetch("/api/job/" + jobId);
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "job lost");
    refs.bar.style.width = (j.percent || 0) + "%";
    const bits = [];
    if (j.speed) bits.push(fmtSpeed(j.speed));
    if (j.eta != null) bits.push(fmtEta(j.eta));
    if (j.percent) bits.unshift(Math.round(j.percent) + "%");
    refs.st.textContent = bits.join(" · ") || "Working…";
    const b = refs.badge;
    if (j.status === "done") {
      b.className = "badge done"; b.textContent = "DONE";
      refs.bar.style.width = "100%";
      refs.st.textContent = "Ready";
      const a = document.createElement("a");
      a.className = "save"; a.href = "/api/file/" + jobId;
      a.textContent = "⬇ Save file";
      a.setAttribute("download", "");
      refs.root.appendChild(a);
      jobs.delete(jobId);
      return;
    }
    if (j.status === "error") {
      b.className = "badge error"; b.textContent = "ERROR";
      refs.st.textContent = "";
      const err = document.createElement("div");
      err.className = "err"; err.textContent = j.error || "Download failed";
      refs.root.appendChild(err);
      jobs.delete(jobId);
      return;
    }
    if (j.status === "processing") { b.className = "badge processing"; b.textContent = "MERGING"; }
    setTimeout(() => pollJob(jobId, refs), 1000);
  } catch (e) {
    setTimeout(() => pollJob(jobId, refs), 3000);
  }
}

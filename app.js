(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };

  const DOMAINS = ["Safety", "Fairness", "Privacy", "Clinical validity", "Regulatory", "Robustness", "Explainability", "Monitoring"];
  const CONTEXT = {
    system: ["Clinical LLM", "Risk classifier", "Imaging model", "Decision support", "Wearable / signals"],
    stage: ["Research", "Prototype", "Pilot", "Production"],
    region: ["US", "EU", "UK", "India"]
  };
  const STARTERS = [
    { tag: "Clinical validity", t: "Sepsis-prediction model", d: "What should I evaluate before deploying it in an ICU?", q: "What should I evaluate for a sepsis-prediction model?" },
    { tag: "Privacy", t: "LLM scribe and HIPAA", d: "Where can PHI leak when notes go through an LLM?", q: "What are the HIPAA and privacy considerations for an LLM clinical scribe?" },
    { tag: "Fairness", t: "Bias audit for dermatology AI", d: "A checklist for subgroup performance and data gaps.", q: "Give me a bias audit checklist for a dermatology image classifier." },
    { tag: "Regulatory", t: "FDA and EU AI Act pathway", d: "Is my software a medical device, and what comes next?", q: "What regulatory pathway applies to clinical decision support software?" }
  ];
  const ICON = {
    copy: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
    redo: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
    up: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 2 2v4h6a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.8 20H7"/></svg>',
    down: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="transform:rotate(180deg)"><path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 2 2v4h6a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.8 20H7"/></svg>',
    del: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>'
  };

  // ---------- state ----------
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }
  };
  let convos = store.get("evalbot.convos", []);
  let activeId = null;
  let context = store.get("evalbot.context", { system: "", stage: "", region: "" });
  let focus = new Set(store.get("evalbot.focus", []));
  let busy = false;
  let selMsg = null, tab = "sources", scopeAll = false, histQuery = "";
  let panelOpen = store.get("evalbot.panel", true);

  const thread = $("#thread"), input = $("#input"), sendBtn = $("#send"), app = $("#app");
  const active = () => convos.find(c => c.id === activeId);
  const save = () => store.set("evalbot.convos", convos.slice(0, 50));

  // ---------- markdown ----------
  const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = s => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\*([^*]+)\*/g, "<i>$1</i>").replace(/\[(\d{1,2})\]/g, '<button type="button" class="cite" data-n="$1" aria-label="Source $1">$1</button>');
  function md(src) {
    const out = []; let list = null;
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const line of src.split("\n")) {
      let m;
      if ((m = line.match(/^(#{2,3}) (.*)/))) { close(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
      else if ((m = line.match(/^[-*] (.*)/))) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = line.match(/^\d+\. (.*)/))) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = line.match(/^> ?(.*)/))) { close(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
      else if (!line.trim()) close();
      else { close(); out.push(`<p>${inline(line)}</p>`); }
    }
    close();
    return out.join("");
  }

  // ---------- theme ----------
  function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
  applyTheme(store.get("evalbot.theme", null));
  $("#themeToggle").onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    applyTheme(next); store.set("evalbot.theme", next);
  };

  // ---------- sidebar ----------
  function renderDomains() {
    const box = $("#domains"); box.innerHTML = "";
    DOMAINS.forEach(d => {
      const b = el("button", "chip", d); b.type = "button";
      b.setAttribute("aria-pressed", focus.has(d));
      b.onclick = () => { focus.has(d) ? focus.delete(d) : focus.add(d); store.set("evalbot.focus", [...focus]); renderDomains(); };
      box.append(b);
    });
  }
  function renderHistory() {
    const box = $("#history"); box.innerHTML = "";
    const q = histQuery.toLowerCase();
    const list = convos.filter(c => !q || c.title.toLowerCase().includes(q) || c.messages.some(m => m.text.toLowerCase().includes(q)));
    if (!list.length) { box.append(el("p", "empty-hist", convos.length ? "No matches." : "Your conversations will appear here.")); return; }
    const day = 864e5, today = new Date().setHours(0, 0, 0, 0);
    const bucket = c => { const t = +c.id; return t >= today ? "Today" : t >= today - day ? "Yesterday" : t >= today - 7 * day ? "Previous 7 days" : "Older"; };
    let last = "";
    list.forEach(c => {
      const b = bucket(c); if (b !== last) { box.append(el("div", "hist-group", b)); last = b; }
      const row = el("div", "hist-item" + (c.id === activeId ? " active" : ""));
      const open = el("button", "open"); open.textContent = c.title; open.onclick = () => { openConvo(c.id); app.classList.remove("open"); };
      const del = el("button", "del", ICON.del); del.setAttribute("aria-label", "Delete conversation"); del.onclick = () => {
        convos = convos.filter(x => x.id !== c.id); save();
        if (activeId === c.id) newChat(); else renderHistory();
      };
      row.append(open, del); box.append(row);
    });
  }
  $("#histSearch").addEventListener("input", e => { histQuery = e.target.value; renderHistory(); });
  $("#aboutBtn").onclick = () => { $("#about").showModal(); app.classList.remove("open"); };
  $("#about").addEventListener("click", e => { if (e.target.id === "about") e.target.close(); });
  $("#menuBtn").onclick = () => app.classList.add("open");
  $("#scrim").onclick = () => app.classList.remove("open");
  $("#newChat").onclick = () => { newChat(); app.classList.remove("open"); input.focus(); };
  const live = !!window.EVALBOT_CONFIG.API_URL;
  $("#modeBadge").textContent = live ? "Live" : "Demo mode";
  $("#modeBadge").classList.toggle("live", live);

  // ---------- context chips ----------
  function renderContext() {
    const row = $("#contextRow"); row.innerHTML = "";
    const labels = { system: "System type", stage: "Stage", region: "Region" };
    Object.keys(CONTEXT).forEach(k => {
      const wrap = el("label", "sel");
      const s = el("select"); s.setAttribute("aria-label", labels[k]);
      s.append(new Option(labels[k], ""));
      CONTEXT[k].forEach(v => s.append(new Option(v, v)));
      s.value = context[k]; s.classList.toggle("set", !!context[k]);
      s.onchange = () => { context[k] = s.value; store.set("evalbot.context", context); s.classList.toggle("set", !!s.value); };
      wrap.append(s); row.append(wrap);
    });
  }

  // ---------- rendering ----------
  const atBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  const scrollDown = () => { thread.scrollTop = thread.scrollHeight; };

  function renderWelcome() {
    thread.innerHTML = "";
    const w = el("div", "col welcome");
    w.innerHTML = `<h2>What should you <em>evaluate</em> before your healthcare AI reaches a patient?</h2>
      <p>Describe your model or tool and I will help you map the safety, fairness, privacy, clinical and regulatory questions worth answering.</p>`;
    const grid = el("div", "starters");
    STARTERS.forEach(s => {
      const b = el("button", "starter", `<small>${s.tag}</small><b>${s.t}</b><span>${s.d}</span>`); b.type = "button";
      b.onclick = () => send(s.q);
      grid.append(b);
    });
    w.append(grid); thread.append(w);
  }

  function checklistCard(cl, state) {
    const total = cl.groups.reduce((n, g) => n + g.items.length, 0);
    const card = el("div", "card");
    const head = el("div", "card-head", `<h4>${esc(cl.title)}</h4><span class="progress"></span>`);
    const bar = el("div", "bar", "<i></i>");
    card.append(head, bar);
    const update = () => {
      const done = state.length ? state.filter(Boolean).length : 0;
      $(".progress", head).textContent = `${done} / ${total}`;
      $("i", bar).style.width = (done / total * 100) + "%";
    };
    let idx = 0;
    cl.groups.forEach(g => {
      const grp = el("div", "group", `<h5>${esc(g.name)}</h5>`);
      g.items.forEach(item => {
        const i = idx++;
        const lab = el("label", "check");
        const cb = el("input"); cb.type = "checkbox"; cb.checked = !!state[i];
        cb.onchange = () => { state[i] = cb.checked; save(); update(); };
        const sp = el("span"); sp.textContent = item;
        lab.append(cb, sp); grp.append(lab);
      });
      card.append(grp);
    });
    const foot = el("div", "card-foot");
    const copy = el("button", "btn-ghost", "Copy as Markdown"); copy.type = "button";
    copy.onclick = () => { clip(checklistMd(cl, state)); flash(copy, "Copied"); };
    const dl = el("button", "btn-ghost", "Download .md"); dl.type = "button";
    dl.onclick = () => download(cl.title.replace(/\W+/g, "-").toLowerCase() + ".md", checklistMd(cl, state));
    foot.append(copy, dl); card.append(foot);
    update();
    return card;
  }
  function checklistMd(cl, state) {
    let i = 0, s = `# ${cl.title}\n`;
    cl.groups.forEach(g => { s += `\n## ${g.name}\n`; g.items.forEach(it => { s += `- [${state[i++] ? "x" : " "}] ${it}\n`; }); });
    return s;
  }

  function botMessage(m, convo, isLast) {
    const msg = el("div", "msg bot"); msg.dataset.i = convo.messages.indexOf(m);
    msg.append(el("div", "who", `<span class="logo">${ICON.logo}</span>EvalBot`));
    const prose = el("div", "prose"); prose.innerHTML = md(m.text); msg.append(prose);
    decorate(msg, m, convo, isLast);
    return msg;
  }
  function decorate(msg, m, convo, isLast) {
    if (m.checklist) { m.checks = m.checks || []; msg.append(checklistCard(m.checklist, m.checks)); }
    if (m.citations && m.citations.length) {
      const chip = el("button", "src-chip"); chip.type = "button";
      chip.textContent = `${m.citations.length} source${m.citations.length > 1 ? "s" : ""} · view`;
      chip.onclick = () => selectMsg(msg.dataset.i, null);
      const row = el("div", "sources"); row.append(chip); msg.append(row);
    }
    const act = el("div", "actions");
    const mk = (icon, label, fn) => { const b = el("button", "icon-btn", icon); b.type = "button"; b.title = label; b.setAttribute("aria-label", label); b.onclick = () => fn(b); return b; };
    act.append(mk(ICON.copy, "Copy", b => { clip(m.text); flash(b, "✓"); }));
    if (isLast) act.append(mk(ICON.redo, "Regenerate", () => regenerate()));
    const up = mk(ICON.up, "Helpful", b => { m.vote = m.vote === "up" ? "" : "up"; save(); up.setAttribute("aria-pressed", m.vote === "up"); dn.setAttribute("aria-pressed", false); });
    const dn = mk(ICON.down, "Not helpful", b => { m.vote = m.vote === "down" ? "" : "down"; save(); dn.setAttribute("aria-pressed", m.vote === "down"); up.setAttribute("aria-pressed", false); });
    up.setAttribute("aria-pressed", m.vote === "up"); dn.setAttribute("aria-pressed", m.vote === "down");
    act.append(up, dn); msg.append(act);
  }
  function userMessage(m) {
    const msg = el("div", "msg user"); const b = el("div", "bubble-user"); b.textContent = m.text; msg.append(b); return msg;
  }

  function renderConvo() {
    const c = active(); $("#chatTitle").textContent = c && c.messages.length ? c.title : "New evaluation";
    if (!c || !c.messages.length) { selMsg = null; renderWelcome(); renderPanel(); return; }
    if (selMsg == null || !c.messages[selMsg]) selMsg = lastBotIndex(c);
    thread.innerHTML = ""; const col = el("div", "col");
    c.messages.forEach((m, i) => col.append(m.role === "user" ? userMessage(m) : botMessage(m, c, i === c.messages.length - 1)));
    thread.append(col); scrollDown(); renderPanel();
  }
  const lastBotIndex = c => { for (let i = c.messages.length - 1; i >= 0; i--) if (c.messages[i].role === "assistant" && c.messages[i].citations?.length) return i; return null; };

  // ---------- actions ----------
  function newChat() { activeId = null; selMsg = null; renderConvo(); renderHistory(); }
  function openConvo(id) { activeId = id; selMsg = null; renderConvo(); renderHistory(); }

  function clip(t) { try { navigator.clipboard.writeText(t); } catch {} }
  function flash(btn, txt) { const o = btn.innerHTML; btn.textContent = txt; setTimeout(() => btn.innerHTML = o, 1200); }
  function download(name, text) {
    const a = el("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" })); a.download = name; a.click(); URL.revokeObjectURL(a.href);
  }
  $("#exportChat").onclick = () => {
    const c = active(); if (!c) return;
    const t = c.messages.map(m => m.role === "user" ? `**You:** ${m.text}` : `**EvalBot:**\n\n${m.text}` + (m.checklist ? "\n\n" + checklistMd(m.checklist, m.checks || []) : "") + (m.citations?.length ? "\n\n### Sources\n" + m.citations.map((r, i) => `${i + 1}. ${fmtRef(r)}`).join("\n") : "")).join("\n\n---\n\n");
    download("evalbot-conversation.md", `# ${c.title}\n\n${t}\n`);
  };

  // Real token streaming when the back end offers /api/chat/stream; the animated
  // reveal is kept as the fallback for demo mode and non-streaming endpoints.
  let activeStream = null;

  async function respond(c) {
    busy = true; sendBtn.disabled = true;
    const col = $(".col", thread) || (thread.innerHTML = "", thread.appendChild(el("div", "col")));
    const msg = el("div", "msg bot");
    msg.append(el("div", "who", `<span class="logo">${ICON.logo}</span>EvalBot`));
    const prose = el("div", "prose", '<div class="typing" aria-label="EvalBot is thinking"><i></i><i></i><i></i></div>');
    msg.append(prose); col.append(msg); scrollDown();

    const payload = c.messages.map(({ role, text }) => ({ role, text }));
    const ctx = { ...context, focus: [...focus] };
    let acc = "", started = false, doneSeen = false;

    const onDelta = chunk => {
      if (!started) { started = true; prose.innerHTML = ""; prose.classList.add("cursor"); }
      acc += chunk;
      const stick = atBottom();
      prose.innerHTML = md(acc);
      if (stick) scrollDown();
    };
    const pushPartial = note => {
      if (!acc) return;
      const m = { role: "assistant", text: acc, checklist: null, citations: [], checks: [] };
      prose.classList.remove("cursor");
      prose.innerHTML = md(acc) + (note ? `<p class="error">${esc(note)}</p>` : "");
      c.messages.push(m); save(); renderConvo();
    };

    try {
      const controller = new AbortController();
      activeStream = controller;
      const streamed = await window.sendStream({
        messages: payload,
        context: ctx,
        signal: controller.signal,
        onMeta: meta => {
          if (!meta || !meta.evidence_count) return;
          const note = el("p", "src-note", `${meta.evidence_count} papers retrieved${meta.degraded && meta.degraded.length ? " · degraded: " + meta.degraded.join(", ") : ""}`);
          msg.insertBefore(note, prose);
        },
        onDelta,
        onDone: done => {
          doneSeen = true;
          if (!acc) onDelta(done.text || "");
          acc = done.text || acc;
          prose.classList.remove("cursor");
          prose.innerHTML = md(acc);
          const m = { role: "assistant", text: acc, checklist: done.checklist, citations: done.citations || [], checks: [] };
          c.messages.push(m); save();
          selMsg = m.citations.length ? c.messages.length - 1 : lastBotIndex(c);
          renderConvo();
        },
        onError: err => { doneSeen = true; pushPartial(`The model stopped early: ${err.message || "stream error"}`); }
      });

      if (!streamed) {
        const reply = await window.sendMessage({ messages: payload, context: ctx });
        const m = { role: "assistant", text: reply.text, checklist: reply.checklist, citations: reply.citations || [], checks: [] };
        prose.innerHTML = ""; prose.classList.add("cursor");
        const words = m.text.split(/(\s+)/);
        let shown = "";
        for (let i = 0; i < words.length; i += 3) {
          shown += words.slice(i, i + 3).join("");
          const stick = atBottom();
          prose.innerHTML = md(shown);
          if (stick) scrollDown();
          await new Promise(r => setTimeout(r, 18));
        }
        prose.classList.remove("cursor");
        c.messages.push(m); save();
        selMsg = m.citations.length ? c.messages.length - 1 : lastBotIndex(c);
        renderConvo();
      } else if (!doneSeen) {
        pushPartial("The connection closed before the answer finished.");
      }
    } catch (e) {
      if (acc) pushPartial("Stream interrupted: " + (e.message || "network error"));
      else prose.innerHTML = `<p class="error">Something went wrong: ${esc(e.message || "request failed")}. Check your connection or API_URL and try again.</p>`;
    } finally {
      activeStream = null;
      busy = false; sendBtn.disabled = !input.value.trim();
    }
  }

  // Escape cancels an in-flight answer (the gateway sees the disconnect and aborts upstream).
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && activeStream) { activeStream.abort(); activeStream = null; }
  });

  function send(text) {
    text = text.trim(); if (!text || busy) return;
    let c = active();
    if (!c) { c = { id: String(Date.now()), title: text.length > 44 ? text.slice(0, 44) + "…" : text, messages: [] }; convos.unshift(c); activeId = c.id; }
    c.messages.push({ role: "user", text });
    save(); renderConvo(); renderHistory();
    input.value = ""; autosize();
    respond(c);
  }
  function regenerate() {
    const c = active(); if (!c || busy) return;
    if (c.messages[c.messages.length - 1].role === "assistant") c.messages.pop();
    renderConvo(); respond(c);
  }


  // ---------- right panel ----------
  const citeIds = r => [r.arxiv && `arXiv:${r.arxiv}`, r.pmid && `PMID ${r.pmid}`, r.doi && `DOI ${r.doi}`].filter(Boolean).join(" · ");
  function refLinks(r) {
    const out = [];
    if (r.arxiv) out.push(["arXiv " + r.arxiv, `https://arxiv.org/abs/${r.arxiv}`]);
    if (r.pmid) out.push(["PubMed " + r.pmid, `https://pubmed.ncbi.nlm.nih.gov/${r.pmid}/`]);
    if (r.doi) out.push(["DOI", `https://doi.org/${r.doi}`]);
    if (r.url) out.push(["Open source", r.url]);
    return out;
  }
  const fmtRef = r => `${r.authors}. ${r.title}. ${r.venue}${r.year ? ", " + r.year : ""}. ${citeIds(r) || r.url || ""}`;

  function refCard(r, n) {
    const card = el("article", "ref"); card.dataset.n = n;
    const top = el("div", "ref-top");
    top.append(el("span", "ref-n", String(n)));
    const bdg = el("span", "badge " + r.type); bdg.textContent = r.type === "pubmed" ? "PubMed" : r.type === "arxiv" ? "arXiv" : "Guidance / law";
    top.append(bdg); card.append(top);
    const h = el("h3"); h.textContent = r.title; card.append(h);
    const meta = el("p", "ref-meta"); meta.textContent = `${r.authors} · ${r.venue}${r.year ? " · " + r.year : ""}`; card.append(meta);
    if (r.claim) { const cl = el("p", "ref-claim", "<b>Cited for</b>"); cl.append(document.createTextNode(r.claim)); card.append(cl); }
    const links = el("div", "ref-links");
    refLinks(r).forEach(([label, href]) => { const a = el("a"); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = label + " ↗"; links.append(a); });
    const cp = el("button", null, "Copy citation"); cp.type = "button"; cp.onclick = () => { clip(fmtRef(r)); flash(cp, "Copied"); };
    links.append(cp); card.append(links);
    return card;
  }

  function renderPanel() {
    const c = active();
    const cur = c && selMsg != null ? c.messages[selMsg] : null;
    const all = [];
    if (c) c.messages.forEach(m => (m.citations || []).forEach(r => { if (!all.some(x => x.ref === r.ref && x.title === r.title)) all.push(r); }));
    $("#srcCount").textContent = scopeAll ? all.length : (cur?.citations?.length || 0);
    $("#tabSources").setAttribute("aria-selected", tab === "sources"); $("#tabSession").setAttribute("aria-selected", tab === "session");
    $("#paneSources").hidden = tab !== "sources"; $("#paneSession").hidden = tab !== "session";

    const ps = $("#paneSources"); ps.innerHTML = "";
    const scope = el("div", "scope");
    [["This answer", false], ["All in chat", true]].forEach(([l, v]) => {
      const b = el("button", "chip", l); b.type = "button"; b.setAttribute("aria-pressed", scopeAll === v);
      b.onclick = () => { scopeAll = v; renderPanel(); }; scope.append(b);
    });
    ps.append(scope);
    const list = scopeAll ? all : (cur?.citations || []);
    if (!list.length) {
      ps.append(el("p", "rp-empty", c ? "This answer cites no sources. Ask a more specific question to get papers and guidance with links." : "Sources for each answer appear here, with arXiv, PubMed and DOI links."));
    } else {
      ps.append(el("p", "rp-note", "Numbers match the [n] markers in the answer. “Cited for” summarises how the source is used; open the link to read the original."));
      list.forEach((r, i) => ps.append(refCard(r, scopeAll ? i + 1 : i + 1)));
    }
    renderSession(c);
  }

  function renderSession(c) {
    const p = $("#paneSession"); p.innerHTML = "";
    if (!c) { p.append(el("p", "rp-empty", "Start a conversation to create a session.")); return; }
    const user = c.messages.filter(m => m.role === "user").length;
    let done = 0, total = 0;
    c.messages.forEach(m => { if (m.checklist) { total += m.checklist.groups.reduce((n, g) => n + g.items.length, 0); done += (m.checks || []).filter(Boolean).length; } });
    const srcs = new Set(); c.messages.forEach(m => (m.citations || []).forEach(r => srcs.add(r.title)));
    const rows = [
      ["Session ID", "S-" + String(c.id).slice(-6)],
      ["Started", new Date(+c.id).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })],
      ["Turns", `${user} question${user === 1 ? "" : "s"}, ${c.messages.length - user} answer${c.messages.length - user === 1 ? "" : "s"}`],
      ["Checklist items", total ? `${done} of ${total} done` : "none yet"],
      ["Sources", `${srcs.size} unique`],
      ["System type", context.system || "not set"], ["Stage", context.stage || "not set"], ["Region", context.region || "not set"],
      ["Focus areas", focus.size ? [...focus].join(", ") : "all"]
    ];
    p.append(el("h2", null, "Current session"));
    const dl = el("dl", "kv"); rows.forEach(([k, v]) => { const dt = el("dt"); dt.textContent = k; const dd = el("dd"); dd.textContent = v; dl.append(dt, dd); }); p.append(dl);
    p.append(el("p", "rp-note", "The system type, stage, region and focus areas are sent with every message in this session. Change them under the message box."));
    const act = el("div", "sess-actions");
    const ren = el("button", "btn-ghost", "Rename"); ren.onclick = () => { const t = prompt("Rename conversation", c.title); if (t && t.trim()) { c.title = t.trim(); save(); renderHistory(); renderConvo(); } };
    const clr = el("button", "btn-ghost", "Clear messages"); clr.onclick = () => { if (confirm("Remove all messages in this session?")) { c.messages = []; save(); newChat(); } };
    act.append(ren, clr); p.append(act);
  }

  function selectMsg(i, n) {
    selMsg = +i; tab = "sources"; scopeAll = false; setPanel(true); renderPanel();
    if (n != null) { const card = $(`.ref[data-n="${n}"]`); if (card) { card.scrollIntoView({ block: "nearest", behavior: "smooth" }); card.classList.add("hl"); setTimeout(() => card.classList.remove("hl"), 1800); } }
  }
  function setPanel(open) {
    panelOpen = open; store.set("evalbot.panel", open);
    const wide = matchMedia("(min-width: 1181px)").matches;
    app.classList.toggle("no-panel", wide && !open); app.classList.toggle("rp-open", !wide && open);
    $("#panelToggle").setAttribute("aria-expanded", open);
  }
  $("#panelToggle").onclick = () => setPanel(!panelOpen);
  $("#panelClose").onclick = () => setPanel(false);
  $("#tabSources").onclick = () => { tab = "sources"; renderPanel(); };
  $("#tabSession").onclick = () => { tab = "session"; renderPanel(); };
  thread.addEventListener("click", e => {
    const b = e.target.closest(".cite"); if (!b) return;
    const msg = b.closest(".msg"); if (msg && msg.dataset.i != null) selectMsg(msg.dataset.i, b.dataset.n);
  });

  // ---------- composer ----------
  function autosize() { input.style.height = "auto"; if (input.value) input.style.height = Math.min(input.scrollHeight, 180) + "px"; sendBtn.disabled = busy || !input.value.trim(); }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); } });
  $("#composer").addEventListener("submit", e => { e.preventDefault(); send(input.value); });
  document.addEventListener("keydown", e => {
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); input.focus(); }
    if (e.key === "Escape") app.classList.remove("open");
  });

  // ---------- init ----------
  setPanel(matchMedia("(min-width: 1181px)").matches ? panelOpen : false); renderDomains(); renderContext(); renderHistory(); renderConvo(); autosize();
})();

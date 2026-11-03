// Single seam between the UI and the back end.
// Set API_URL to your endpoint to leave mock mode.
// Contract: POST { messages: [{role, text}], context: {...} }
//   -> JSON { text: string (markdown; use [1], [2] markers),
//             checklist?: {title, groups:[{name, items:[string]}]},
//             citations?: [{type:'arxiv'|'pubmed'|'web', title, authors, venue, year, arxiv?, pmid?, doi?, url?, claim}] }
// citations[i] is shown as source number i+1 in the right-hand panel.
window.EVALBOT_CONFIG = window.EVALBOT_CONFIG || { API_URL: "" };


// Reference library for demo mode. Identifiers (arXiv / PMID / DOI) are the stable part;
// "claim" on each citation is a paraphrase of what the answer uses the source for, not a quotation.
const REFS = {
  obermeyer: { type: "pubmed", title: "Dissecting racial bias in an algorithm used to manage the health of populations", authors: "Obermeyer Z, Powers B, Vogeli C, Mullainathan S", venue: "Science 366(6464):447-453", year: 2019, pmid: "31649194", doi: "10.1126/science.aax2342" },
  rajkomar: { type: "pubmed", title: "Ensuring Fairness in Machine Learning to Advance Health Equity", authors: "Rajkomar A, Hardt M, Howell MD, Corrado G, Chin MH", venue: "Annals of Internal Medicine 169(12):866-872", year: 2018, pmid: "30508424" },
  daneshjou: { type: "arxiv", title: "Disparities in dermatology AI performance on a diverse, curated clinical image set", authors: "Daneshjou R, et al.", venue: "Science Advances 8(32)", year: 2022, arxiv: "2203.08807" },
  mitchell: { type: "arxiv", title: "Model Cards for Model Reporting", authors: "Mitchell M, et al.", venue: "ACM FAT* Conference", year: 2019, arxiv: "1810.03993" },
  carlini: { type: "arxiv", title: "Extracting Training Data from Large Language Models", authors: "Carlini N, et al.", venue: "USENIX Security Symposium", year: 2021, arxiv: "2012.07805" },
  shokri: { type: "arxiv", title: "Membership Inference Attacks against Machine Learning Models", authors: "Shokri R, Stronati M, Song C, Shmatikov V", venue: "IEEE Symposium on Security and Privacy", year: 2017, arxiv: "1610.05820" },
  hhsdeid: { type: "web", title: "Guidance Regarding Methods for De-identification of PHI under the HIPAA Privacy Rule", authors: "U.S. HHS Office for Civil Rights", venue: "hhs.gov", year: "", url: "https://www.hhs.gov/hipaa/for-professionals/special-topics/de-identification/index.html" },
  gdpr: { type: "web", title: "Regulation (EU) 2016/679 (GDPR), Art. 9 and Art. 35", authors: "European Union", venue: "EUR-Lex", year: 2016, url: "https://eur-lex.europa.eu/eli/reg/2016/679/oj" },
  medhalt: { type: "arxiv", title: "Med-HALT: Medical Domain Hallucination Test for Large Language Models", authors: "Pal A, Umapathi LK, Sankarasubbu M", venue: "CoNLL", year: 2023, arxiv: "2307.15343" },
  healthbench: { type: "arxiv", title: "HealthBench: Evaluating Large Language Models Towards Improved Human Health", authors: "Arora RK, et al.", venue: "arXiv preprint", year: 2025, arxiv: "2505.08775" },
  medalign: { type: "arxiv", title: "MedAlign: A Clinician-Generated Dataset for Instruction Following with Electronic Medical Records", authors: "Fleming SL, et al.", venue: "AAAI", year: 2023, arxiv: "2308.14089" },
  singhal: { type: "pubmed", title: "Large language models encode clinical knowledge", authors: "Singhal K, et al.", venue: "Nature 620:172-180", year: 2023, pmid: "37438534", arxiv: "2212.13138" },
  ji: { type: "arxiv", title: "Survey of Hallucination in Natural Language Generation", authors: "Ji Z, et al.", venue: "ACM Computing Surveys 55(12)", year: 2023, arxiv: "2202.03629" },
  fda: { type: "web", title: "Artificial Intelligence and Machine Learning in Software as a Medical Device", authors: "U.S. Food and Drug Administration", venue: "fda.gov", year: "", url: "https://www.fda.gov/medical-devices/software-medical-device-samd/artificial-intelligence-and-machine-learning-software-medical-device" },
  aiact: { type: "web", title: "Regulation (EU) 2024/1689 (Artificial Intelligence Act)", authors: "European Union", venue: "EUR-Lex", year: 2024, url: "https://eur-lex.europa.eu/eli/reg/2024/1689/oj" },
  tripod: { type: "pubmed", title: "TRIPOD+AI statement: updated guidance for reporting clinical prediction models that use regression or machine learning methods", authors: "Collins GS, et al.", venue: "BMJ 385:e078378", year: 2024, pmid: "38626948" },
  wong: { type: "pubmed", title: "External Validation of a Widely Implemented Proprietary Sepsis Prediction Model in Hospitalized Patients", authors: "Wong A, et al.", venue: "JAMA Internal Medicine 181(8):1065-1070", year: 2021, pmid: "34152373" },
  vickers: { type: "pubmed", title: "Decision curve analysis: a novel method for evaluating prediction models", authors: "Vickers AJ, Elkin EB", venue: "Medical Decision Making 26(6):565-574", year: 2006, pmid: "17099194" }
};
const cite = (ref, claim) => ({ ...REFS[ref], ref, claim });

const MOCK = {
  fairness: {
    text: `## Auditing a healthcare model for bias

Performance averaged over a whole population can hide large gaps for specific groups. Start by deciding **which groups matter clinically**, then measure each one separately.

### What to look at
- **Subgroup performance:** sensitivity, specificity, PPV and calibration by age, sex, race/ethnicity, language, payer and site.
- **Label bias:** if labels come from past clinical decisions or billing codes, they may encode unequal access to care, not true disease. [1]
- **Representation:** compare training data demographics with the intended-use population. [2][3]
- **Intersectional slices:** check combinations (e.g. older women on Medicaid), not only single attributes.

> A model can be well calibrated overall and badly calibrated for a subgroup. Report calibration curves per group.

Document the result and known limits in a **model card** [4].`,
    checklist: {
      title: "Fairness evaluation checklist",
      groups: [
        { name: "Data", items: ["Document demographics of train, validation and test sets", "Check for missing or proxy-coded sensitive attributes", "Review how labels were generated and who was under-served", "Confirm external validation on a different site or population"] },
        { name: "Metrics", items: ["Report sensitivity, specificity, PPV, AUROC per subgroup with confidence intervals", "Plot calibration curves per subgroup", "Pick fairness criteria (equal opportunity, calibration parity) and justify the choice", "Test intersectional slices with enough sample size"] },
        { name: "Mitigation and monitoring", items: ["Document mitigation tried and its trade-offs", "Set subgroup alert thresholds for post-deployment drift", "Publish a model card with known limitations"] }
      ]
    },
    citations: [
      cite("obermeyer", "A widely used care-management algorithm underestimated Black patients' needs because it used cost as a proxy label."),
      cite("rajkomar", "Framework for where bias enters the ML pipeline in healthcare and how to assess equity of performance."),
      cite("daneshjou", "Dermatology AI performance dropped on darker skin tones in a diverse curated image set."),
      cite("mitchell", "Template for documenting intended use, subgroup metrics and limitations.")
    ]
  },
  privacy: {
    text: `## Privacy and security for patient data

Whether you are covered by **HIPAA**, **GDPR** or both depends on who you are acting for and where the patients are. Settle that before you design the pipeline [4].

### Key questions
- Is the data **PHI** or truly de-identified (Safe Harbor vs. Expert Determination) [1]?
- Do prompts, logs, embeddings or fine-tuning sets ever contain identifiers?
- Does each vendor, including your model host, sign a **BAA** or DPA?
- Can the model **memorise and leak** training records [2][3]?

### Common failure points
1. Free-text notes pasted into prompts with names and MRNs intact.
2. Application logs and analytics capturing full requests.
3. Re-identification by linking "anonymous" records with other datasets.`,
    checklist: {
      title: "Privacy & security checklist",
      groups: [
        { name: "Governance", items: ["Determine HIPAA / GDPR / local law applicability", "Sign BAAs or DPAs with every processor", "Complete a DPIA or privacy risk assessment", "Define retention and deletion rules"] },
        { name: "Technical controls", items: ["Encrypt data in transit and at rest", "Apply least-privilege access and audit logging", "Scrub PHI from logs, traces and analytics", "Test for training-data extraction and membership inference"] }
      ]
    },
    citations: [
      cite("hhsdeid", "Defines the two HIPAA de-identification routes: Safe Harbor and Expert Determination."),
      cite("carlini", "Showed verbatim training examples can be extracted from a language model."),
      cite("shokri", "Introduced membership-inference attacks that reveal whether a record was in training data."),
      cite("gdpr", "Health data is special-category data (Art. 9); high-risk processing needs a DPIA (Art. 35).")
    ]
  },
  llm: {
    text: `## Evaluating an LLM-based clinical tool

For generative systems the main risks are different from those of a classifier: **hallucination, omission, unsafe advice and inconsistent output** [1][5].

### Evaluate at three levels
- **Component:** factuality, citation accuracy, omission of critical findings [2].
- **Workflow:** does a clinician catch errors? Measure editing time and automation bias.
- **Outcome:** safety events, time saved, patient impact.

### Methods that hold up
- Clinician-written gold standards with **blind, multi-rater** scoring and agreement stats [3][4].
- Adversarial and red-team prompts covering self-harm, dosing and emergencies.
- **LLM-as-judge** only after validating the judge against clinicians.`,
    checklist: {
      title: "LLM evaluation checklist",
      groups: [
        { name: "Quality and safety", items: ["Measure hallucination and omission rates on real, de-identified encounters", "Score harm severity with a clinician rubric", "Red-team high-risk scenarios (dosing, triage, suicidality)", "Check consistency across repeated runs and paraphrased prompts"] },
        { name: "Human factors", items: ["Study clinician override and automation bias", "Show uncertainty and source links in the UI", "Define escalation when the model is out of scope"] },
        { name: "Operations", items: ["Pin model versions and re-run evals on every change", "Log inputs/outputs securely for incident review"] }
      ]
    },
    citations: [
      cite("medhalt", "Benchmark for measuring hallucination in medical question answering."),
      cite("healthbench", "Rubric-based evaluation of LLM health responses written with physician input."),
      cite("medalign", "Clinician-written instructions on real EHR data as an evaluation set."),
      cite("singhal", "Example of multi-axis human evaluation of LLM clinical answers (factuality, harm, bias)."),
      cite("ji", "Taxonomy and survey of hallucination causes and metrics in text generation.")
    ]
  },
  regulatory: {
    text: `## Regulatory pathway basics

Your **intended use** determines almost everything. Software that informs, drives or diagnoses clinical decisions is often a medical device [1].

### By region
- **US (FDA):** SaMD classification, 510(k) / De Novo, and a **Predetermined Change Control Plan** for models that update.
- **EU:** MDR plus the **AI Act**; high-risk obligations [2] include risk management, data governance and human oversight.
- **UK:** UKCA / MHRA software and AI as a medical device programme.
- **India:** CDSCO medical device rules; also DPDP Act for data.

Talk to regulatory counsel early. This is a map, not legal advice.`,
    checklist: {
      title: "Regulatory readiness checklist",
      groups: [
        { name: "Scoping", items: ["Write a precise intended-use statement", "Classify the device per target jurisdiction", "Identify clinical-decision-support exemptions that may apply"] },
        { name: "Evidence", items: ["Plan analytical and clinical validation", "Maintain a risk file (ISO 14971) and software lifecycle docs (IEC 62304)", "Draft a change-control plan for model updates"] }
      ]
    },
    citations: [
      cite("fda", "FDA overview page for AI/ML-enabled software as a medical device, including change-control guidance."),
      cite("aiact", "Primary legal text for high-risk AI system obligations in the EU.")
    ]
  },
  clinical: {
    text: `## Evaluating a clinical prediction model

A strong AUROC on a retrospective test set is the **start** of validation, not the end.

### Beyond discrimination
- **Calibration:** do predicted risks match observed rates?
- **Clinical utility:** decision-curve analysis shows net benefit at realistic thresholds [3].
- **Alert burden:** PPV and alerts per patient-day decide whether clinicians will act on it.
- **Temporal and external validity:** test on a later period and different hospitals [2].

### Watch for leakage
Features recorded *after* the clinical event (e.g. antibiotics ordered, for a sepsis model) make results look better than they will be in practice [2].`,
    checklist: {
      title: "Clinical validation checklist",
      groups: [
        { name: "Study design", items: ["Define prediction time-point and prevent label/feature leakage", "Use temporal split plus external site validation", "Follow TRIPOD+AI for reporting"] },
        { name: "Performance", items: ["Report discrimination and calibration with CIs", "Run decision-curve analysis", "Estimate alert volume and PPV at chosen threshold"] },
        { name: "Deployment", items: ["Run a silent/shadow trial before go-live", "Monitor drift and recalibrate on a schedule"] }
      ]
    },
    citations: [
      cite("tripod", "Reporting guideline for prediction-model studies that use machine learning."),
      cite("wong", "External validation found a deployed sepsis model performed far worse than the vendor reported."),
      cite("vickers", "Decision curve analysis: net benefit across decision thresholds.")
    ]
  },
  fallback: {
    text: `## A starting framework

Healthcare AI evaluation usually covers six areas. Tell me your system type, stage and region (use the chips under the message box) and I will tailor this.

1. **Clinical validity:** does it work for the intended use and population?
2. **Safety and robustness:** how does it fail, and how bad is that?
3. **Fairness:** are errors distributed equitably?
4. **Privacy and security:** how is patient data protected end to end?
5. **Regulatory and ethical:** what approvals and oversight apply?
6. **Monitoring:** how will you detect drift after launch?

Ask about any of these, or try a prompt like *"What should I evaluate for a sepsis-prediction model?"*`,
    checklist: {
      title: "Evaluation overview",
      groups: [
        { name: "Before you build", items: ["Define intended use, users and clinical setting", "Name the harms you most need to avoid", "Choose metrics tied to clinical outcomes"] },
        { name: "Before you launch", items: ["Validate externally and by subgroup", "Complete privacy and security review", "Confirm regulatory classification"] }
      ]
    },
    citations: []
  }
};

function pickMock(q) {
  q = q.toLowerCase();
  if (/bias|fair|equit|disparit|subgroup|demograph/.test(q)) return MOCK.fairness;
  if (/hipaa|privacy|gdpr|phi\b|de-?identif|security|consent/.test(q)) return MOCK.privacy;
  if (/llm|language model|scribe|gpt|hallucin|chatbot|generative/.test(q)) return MOCK.llm;
  if (/fda|regulat|ce mark|mdr|ai act|samd|approval|compliance/.test(q)) return MOCK.regulatory;
  if (/sepsis|predict|classif|imaging|validat|clinical|auroc|calibrat/.test(q)) return MOCK.clinical;
  return MOCK.fallback;
}

window.sendMessage = async function sendMessage({ messages, context }) {
  const url = window.EVALBOT_CONFIG.API_URL;
  if (url) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, context })
    });
    if (!res.ok) throw new Error("Request failed (" + res.status + ")");
    return res.json();
  }
  await new Promise(r => setTimeout(r, 600));
  const last = messages[messages.length - 1].text;
  let reply = pickMock(last);
  const ctx = [context.system, context.stage, context.region].filter(Boolean);
  if (ctx.length) {
    reply = { ...reply, text: reply.text.replace(/^(## .*)$/m, `$1\n\n*Tailored for: ${ctx.join(" · ")}*`) };
  }
  return reply;
};

// ── streaming transport ────────────────────────────────────────────────────
// Posts to /api/chat/stream and consumes SSE frames: meta -> delta* -> done|error.
// Returns false when the endpoint is missing or the browser cannot stream, so the
// caller can fall back to window.sendMessage() (non-streaming) untouched.
window.sendStream = async function sendStream({ messages, context, onMeta, onDelta, onDone, onError, signal }) {
  const cfg = window.EVALBOT_CONFIG;
  const url = cfg.STREAM_URL || (cfg.API_URL ? String(cfg.API_URL).replace(/\/api\/chat\/?$/, "/api/chat/stream") : "");
  if (!url || typeof ReadableStream === "undefined" || !ReadableStream.prototype.getReader) return false;

  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, context }),
    signal
  });
  if (res.status === 404 || res.status === 501 || res.status === 405) return false;
  if (!res.ok || !res.body) throw new Error("Request failed (" + res.status + ")");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let settled = false;

  const dispatch = frame => {
    const lines = frame.split(/\r?\n/);
    let name = "message";
    const data = [];
    for (const line of lines) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (!data.length) return;
    let payload;
    try { payload = JSON.parse(data.join("\n")); } catch { return; }
    if (name === "meta" && onMeta) onMeta(payload);
    else if (name === "delta" && onDelta) onDelta(payload.text || "");
    else if (name === "done") { settled = true; if (onDone) onDone(payload); }
    else if (name === "error") { settled = true; if (onError) onError(payload); }
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (frame.trim()) dispatch(frame);
      }
    }
    if (buffer.trim()) dispatch(buffer);
  } finally {
    try { reader.releaseLock(); } catch (_) {}
  }
  return settled ? true : "closed";
};

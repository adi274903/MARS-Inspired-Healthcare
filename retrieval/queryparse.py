"""Deterministic question -> (intent, keyword filters, FTS terms).

Deliberately rule-based: routing must be reproducible and free. Filters are
*keywords*, not exact vocabulary, so they survive dataset vocabulary drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "to", "and", "or", "is", "are", "was", "were", "how", "many",
    "much", "what", "which", "who", "when", "where", "why", "do", "does", "did", "with", "without", "that",
    "this", "these", "those", "i", "we", "you", "it", "its", "be", "been", "being", "have", "has", "had",
    "about", "there", "their", "from", "by", "as", "at", "into", "than", "then", "so", "such", "can", "could",
    "should", "would", "may", "might", "will", "any", "all", "most", "more", "less", "some", "list", "show",
    "give", "me", "tell", "papers", "paper", "studies", "study", "articles", "article", "please", "using",
    "use", "used", "uses", "per", "vs", "versus",
}

# facet column -> keyword aliases found in a question
FACET_TERMS: dict[str, dict[str, str]] = {
    "a_domain": {
        "oncology": "oncology", "cancer": "oncology", "tumour": "oncology", "tumor": "oncology",
        "cardiovascular": "cardiovascular", "cardiac": "cardiovascular", "heart": "cardiovascular",
        "neurology": "neurology", "neuro": "neurology", "stroke": "neurology",
        "surgery": "surgery", "anesthesia": "surgery", "anaesthesia": "surgery", "perioperative": "surgery",
        "emergency": "emergency", "critical care": "emergency", "icu": "emergency", "intensive care": "emergency",
        "sepsis": "sepsis", "covid": "covid", "radiology": "radiology", "imaging": "radiology",
        "mental health": "mental", "psychiatry": "mental", "depression": "mental",
        "ophthalmology": "ophthalm", "retina": "ophthalm", "dermatology": "dermat",
        "pathology": "patholog", "gastro": "gastro", "pediatric": "pediatric", "paediatric": "pediatric",
        "obstetric": "obstetric", "gynecology": "gyneco", "nephrology": "nephro", "kidney": "nephro",
        "endocrine": "endocrine", "diabetes": "diabet",
    },
    "b_end_user": {
        "radiologist": "radiologist", "clinician": "clinician", "physician": "clinician", "doctor": "clinician",
        "patient": "patient", "caregiver": "patient", "nurse": "clinician", "administrator": "administrator",
        "operations team": "administrator", "educator": "educator", "trainee": "educator",
        "public health": "public health",
    },
    "c_use_case": {
        "prognosis": "prognosis", "risk prediction": "prognosis", "survival prediction": "prognosis",
        "diagnosis": "diagnosis", "triage": "triage", "screening": "screening", "early detection": "screening",
        "monitoring": "monitoring", "management": "management", "workflow": "workflow",
        "research support": "research support",
    },
    "d_analytics": {
        "generative": "generative", "llm": "generative", "language model": "generative",
        "chatbot": "generative", "gpt": "generative", "gemma": "generative",
        "predictive": "predictive", "descriptive": "descriptive", "prescriptive": "prescriptive",
    },
    "e_formulation": {
        "binary classification": "binary", "classification": "classification", "survival": "survival",
        "time-to-event": "survival", "segmentation": "segmentation", "regression": "regression",
        "text generation": "text generation", "multi-label": "multi-class", "multiclass": "multi-class",
    },
    "f_algorithm": {
        "xgboost": "classical ml", "random forest": "classical ml", "svm": "classical ml",
        "logistic regression": "classical", "deep learning": "deep learning", "cnn": "deep learning",
        "transformer": "deep learning", "foundation model": "foundation", "llm": "foundation",
        "rule-based": "rule-based", "statistical": "statistical",
    },
    "g_eval_tier": {
        "external validation": "external", "externally validated": "external", "external cohort": "external",
        "temporal validation": "temporal", "prospective": "prospective", "silent deployment": "prospective",
        "randomized": "randomized", "randomised": "randomized", "clinical trial": "clinical trial",
        "internal validation": "internal", "internal split": "internal", "benchmark": "benchmark",
        "in silico": "silico",
    },
}

VALIDATION_FLAGS = {
    "external_only": (r"external(ly)? validat|externally validated|external cohort", "external_val"),
    "temporal_only": (r"temporal validat", "temporal_val"),
    "deployed_only": (r"deploy(ed|ment) in practice|live deployment|in production", "deployed"),
    "rct_only": (r"randomi[sz]ed (controlled )?trial|\brct\b", "rct"),
    "with_ci": (r"confidence interval", "ci_reported"),
    "calibrated": (r"calibrat", "calibration_reported"),
}

AGGREGATE_CUE = re.compile(
    r"\bhow many\b|\bhow often\b|\bwhat (proportion|percentage|share|fraction)\b|\bcount\b|"
    r"\bnumber of\b|\bdistribution\b|\bbreakdown\b|\bmost common\b|\btrend\b|\bper year\b|\bby year\b",
    re.I,
)
MULTIHOP_CUE = re.compile(
    r"\bsame (author|team|group)s?\b|\bco-?author|\bwhich journals?\b|\bwho (else|has|published)\b|"
    r"\bco-?occur|\bsimilar papers\b|\brelated papers\b|\bnetwork\b|\bcollaborat|\bmesh\b|\bmeSH terms\b",
    re.I,
)
SEMANTIC_CUE = re.compile(
    r"\bhow (do|does|should|would|can)\b|\bapproach(es)?\b|\bframework\b|\bchecklist\b|\bwhat should\b|"
    r"\bbest practice\b|\bevaluate\b|\bconsiderations?\b|\bpitfalls?\b",
    re.I,
)

GROUP_BY_CUES = [
    (re.compile(r"per year|by year|each year|annual|over time|trend", re.I), "year"),
    (re.compile(r"by (clinical )?domain|per domain|each domain|which domain", re.I), "a_domain"),
    (re.compile(r"by (use )?case|per use case|which use case", re.I), "c_use_case"),
    (re.compile(r"by (evaluation )?tier|by validation type|which validation", re.I), "g_eval_tier"),
    (re.compile(r"by algorithm|which algorithm|by model type", re.I), "f_algorithm"),
    (re.compile(r"by (end )?user|which user", re.I), "b_end_user"),
    (re.compile(r"by formulation|by task", re.I), "e_formulation"),
    (re.compile(r"by analytics|by (ai )?type", re.I), "d_analytics"),
]


@dataclass
class ParsedQuery:
    raw: str
    intent: str = "hybrid"
    filters: dict[str, list[str]] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    year_min: int | None = None
    year_max: int | None = None
    graph_template: str | None = None
    graph_params: dict = field(default_factory=dict)
    fts_terms: list[str] = field(default_factory=list)
    group_by: str | None = None

    def as_dict(self) -> dict:
        return {
            "raw": self.raw, "intent": self.intent, "filters": self.filters, "flags": self.flags,
            "year_min": self.year_min, "year_max": self.year_max, "graph_template": self.graph_template,
            "graph_params": self.graph_params, "fts_terms": self.fts_terms, "group_by": self.group_by,
        }


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _extract_years(question: str) -> tuple[int | None, int | None]:
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", question)]
    lo = hi = None
    if years:
        lo, hi = min(years), max(years)
    if re.search(r"\bsince\s+(19|20)\d{2}", question, re.I) and lo:
        hi = None
    if re.search(r"\b(until|before)\s+(19|20)\d{2}", question, re.I) and hi:
        lo, hi = None, hi
    if re.search(r"\b(last|past)\s+(\d+)\s+years?\b", question, re.I):
        import datetime

        n = int(re.search(r"\b(?:last|past)\s+(\d+)\s+years?\b", question, re.I).group(1))
        lo, hi = datetime.date.today().year - n, None
    return lo, hi


def _graph_template(q: str) -> tuple[str | None, dict]:
    low = q.lower()
    if "same author" in low or "co-author" in low or "coauthor" in low or "collaborat" in low:
        return "coauthor_network", {}
    if "which journal" in low or "journals" in low:
        return "journal_rollup", {}
    if "co-occur" in low or "cooccur" in low or "mesh" in low:
        return "mesh_cooccurrence", {}
    if "similar" in low or "related papers" in low:
        return "similar_by_entities", {}
    if "who else" in low or "who has" in low or "who published" in low:
        return "papers_by_author", {}
    return None, {}


def parse(question: str) -> ParsedQuery:
    q = _normalise(question)
    parsed = ParsedQuery(raw=question)

    for column, aliases in FACET_TERMS.items():
        hits: list[str] = []
        for phrase, keyword in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", q) and keyword not in hits:
                hits.append(keyword)
        if hits:
            parsed.filters[column] = hits[:3]

    for flag, (pattern, _col) in VALIDATION_FLAGS.items():
        if re.search(pattern, q, re.I):
            parsed.flags[flag] = True

    # A validation cue sets BOTH a tier keyword ("external") and a boolean flag. The flag is
    # the faithful signal — the tier text misses rows labelled e.g. "Temporal validation" that
    # also carry external_val=1 — so drop the redundant keyword instead of ANDing the two,
    # which silently undercounts (4,554 vs 4,655 on the real corpus).
    for flag, keyword in (("external_only", "external"), ("temporal_only", "temporal")):
        if parsed.flags.get(flag) and "g_eval_tier" in parsed.filters:
            remaining = [value for value in parsed.filters["g_eval_tier"] if value != keyword]
            if remaining:
                parsed.filters["g_eval_tier"] = remaining
            else:
                parsed.filters.pop("g_eval_tier", None)

    parsed.year_min, parsed.year_max = _extract_years(question)

    for pattern, column in GROUP_BY_CUES:
        if pattern.search(q):
            parsed.group_by = column
            break

    tokens = [
        t for t in re.findall(r"[a-z0-9][a-z0-9\-]{1,}", q)
        if t not in STOPWORDS and len(t) > 2
    ]
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
    parsed.fts_terms = seen[:12]

    template, params = _graph_template(q)
    parsed.graph_template = template
    parsed.graph_params = params

    if AGGREGATE_CUE.search(q):
        parsed.intent = "aggregate"
        if parsed.group_by is None and re.search(r"\bby (clinical )?domain\b|which domain|domain", q):
            parsed.group_by = "a_domain"
    elif MULTIHOP_CUE.search(q) or template:
        parsed.intent = "multihop"
        parsed.graph_template = template or "papers_by_author"
    elif SEMANTIC_CUE.search(q) and not parsed.filters:
        parsed.intent = "semantic"
    elif parsed.filters or parsed.flags or parsed.year_min or parsed.year_max:
        parsed.intent = "faceted"
    else:
        parsed.intent = "hybrid"
    return parsed

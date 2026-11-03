"""Graph retrieval. Cypher is never model-generated: named templates only.

Two implementations, same interface:
* ``Neo4jGraph``  - Cypher templates against Neo4j (only when NEO4J_URI is set).
* ``LocalGraph``  - the same templates evaluated over SQLite rows in Python, so the
  graph leg works without a graph server (and tests never need one).
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol

from retrieval.queryparse import ParsedQuery
from retrieval.sqlite_store import PaperStore, _json_list

TEMPLATES: dict[str, dict[str, Any]] = {
    "papers_by_author": {
        "cypher": (
            "MATCH (a:Author)-[:AUTHORED]->(p:Paper) "
            "WHERE toLower(a.name) CONTAINS toLower($author) "
            "RETURN p.pmid AS pmid, p.year AS year ORDER BY p.year DESC LIMIT $limit"
        ),
        "params": ("author", "limit"),
    },
    "coauthor_network": {
        "cypher": (
            "MATCH (a:Author)-[:AUTHORED]->(p:Paper)<-[:AUTHORED]-(b:Author) "
            "WHERE toLower(a.name) CONTAINS toLower($author) AND a <> b "
            "RETURN b.name AS name, count(DISTINCT p) AS n ORDER BY n DESC LIMIT $limit"
        ),
        "params": ("author", "limit"),
    },
    "mesh_cooccurrence": {
        "cypher": (
            "MATCH (m1:MeSH)<-[:HAS_MESH]-(p:Paper)-[:HAS_MESH]->(m2:MeSH) "
            "WHERE toLower(m1.name) CONTAINS toLower($mesh) AND m1 <> m2 "
            "RETURN m2.name AS name, count(DISTINCT p) AS n ORDER BY n DESC LIMIT $limit"
        ),
        "params": ("mesh", "limit"),
    },
    "journal_rollup": {
        "cypher": (
            "MATCH (p:Paper)-[:PUBLISHED_IN]->(j:Journal) "
            "RETURN j.name AS name, count(p) AS n ORDER BY n DESC LIMIT $limit"
        ),
        "params": ("limit",),
    },
    "similar_by_entities": {
        "cypher": (
            "MATCH (p:Paper {pmid: $pmid})-[:HAS_MESH|AUTHORED]-(shared)-[]-(other:Paper) "
            "WHERE other <> p RETURN other.pmid AS pmid, count(*) AS n ORDER BY n DESC LIMIT $limit"
        ),
        "params": ("pmid", "limit"),
    },
}


def render(template: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (cypher, params) for a named template. User text is only ever a parameter."""
    spec = TEMPLATES[template]
    unknown = set(params) - set(spec["params"])
    if unknown:
        raise KeyError(f"unknown params for template {template}: {sorted(unknown)}")
    safe = {k: v for k, v in params.items() if k in spec["params"]}
    return spec["cypher"], safe


class GraphRetriever(Protocol):
    def available(self) -> bool: ...
    def query(self, template: str, params: dict[str, Any]) -> dict[str, Any]: ...


class LocalGraph:
    """Same templates, evaluated over the SQLite rows (no graph server needed)."""

    def __init__(self, store: PaperStore) -> None:
        self.store = store

    def available(self) -> bool:
        return self.store.available()

    def query(self, template: str, params: dict[str, Any]) -> dict[str, Any]:
        render(template, params)  # validates template/params before touching data
        limit = int(params.get("limit", 20))
        if template == "papers_by_author":
            pmids = self.store.by_author(str(params.get("author", "")), limit)
            return {"template": template, "pmids": pmids, "rows": []}
        if template == "coauthor_network":
            pmids = self.store.by_author(str(params.get("author", "")), limit)
            rows = self._coauthors(pmids, str(params.get("author", "")).lower(), limit)
            return {"template": template, "pmids": pmids, "rows": rows}
        if template == "mesh_cooccurrence":
            pmids = self.store.by_mesh(str(params.get("mesh", "")), limit)
            rows = self._mesh_cooccurrence(pmids, str(params.get("mesh", "")).lower(), limit)
            return {"template": template, "pmids": pmids, "rows": rows}
        if template == "journal_rollup":
            return {"template": template, "pmids": [], "rows": self.store.journal_rollup(limit)}
        if template == "similar_by_entities":
            pmid = str(params.get("pmid", ""))
            return {"template": template, "pmids": self.store.similar(pmid, limit), "rows": []}
        raise KeyError(f"unhandled template {template}")

    def _coauthors(self, pmids: list[str], author: str, limit: int) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in self.store.get_many(pmids).values():
            for name in _json_list(row.get("authors")):
                if author and author in name.lower():
                    continue
                counts[name] = counts.get(name, 0) + 1
        return [{"name": k, "n": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]

    def _mesh_cooccurrence(self, pmids: list[str], mesh: str, limit: int) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in self.store.get_many(pmids).values():
            for term in _json_list(row.get("mesh_names")):
                if mesh and mesh in term.lower():
                    continue
                counts[term] = counts.get(term, 0) + 1
        return [{"name": k, "n": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


class Neo4jGraph:
    """Cypher templates against Neo4j. Read-only queries, parameterised."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        self.uri, self.user, self.password = uri, user, password
        self._driver: Any = None

    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self._driver

    def available(self) -> bool:
        if not self.uri:
            return False
        try:
            self.driver().verify_connectivity()
            return True
        except Exception:
            return False

    def query(self, template: str, params: dict[str, Any]) -> dict[str, Any]:
        cypher, safe = render(template, params)
        records, _summary, _keys = self.driver().execute_query(cypher, **safe)
        rows = [dict(record) for record in records]
        pmids = [str(r["pmid"]) for r in rows if r.get("pmid")]
        return {"template": template, "pmids": pmids, "rows": rows}

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None


def extract_graph_params(parsed: ParsedQuery, question: str) -> dict[str, Any]:
    """Derive template parameters from the question. Names come from the text, never SQL."""
    template = parsed.graph_template or "papers_by_author"
    params: dict[str, Any] = {"limit": 20}
    name = _proper_noun(question) or (parsed.fts_terms[0] if parsed.fts_terms else "")
    if template in {"papers_by_author", "coauthor_network"}:
        params["author"] = name
    elif template == "mesh_cooccurrence":
        params["mesh"] = _mesh_term(question) or name
    elif template == "similar_by_entities":
        params["pmid"] = str(parsed.graph_params.get("pmid", ""))
    return params


_NAME = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2})\b")
_MESH = re.compile(r"\b(?:mesh|meSH)\b[^a-z]*([A-Za-z][A-Za-z ,\-]{2,40})", re.I)


def _proper_noun(question: str) -> str:
    for match in _NAME.finditer(question):
        candidate = match.group(1)
        if candidate.lower() in {"how", "what", "which", "who", "the", "papers", "same", "external"}:
            continue
        if len(candidate.split()) >= 1:
            return candidate
    return ""


def _mesh_term(question: str) -> str:
    match = _MESH.search(question)
    return match.group(1).strip() if match else ""

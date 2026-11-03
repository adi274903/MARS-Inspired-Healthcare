"""xlsx -> ``data/papers.jsonl`` with the standard library only.

The workbook is a ~28 MB zip whose ``xl/sharedStrings.xml`` part alone is ~80 MB
once inflated, so ``openpyxl``/``pandas`` are out (both are absent from the
runtime, and both would materialise the whole sheet). ``zipfile`` +
``xml.etree.ElementTree.iterparse`` stream it: shared strings are consumed
first into a flat list, then sheet1 is walked row by row and every row element
is cleared as soon as it has been converted.

The dataset is only ever opened when this module is run as a CLI.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SHEET_PART = "xl/worksheets/sheet1.xml"
SHARED_PART = "xl/sharedStrings.xml"

# Columns that carry no information for the corpus.
DROP_COLUMNS = {"Unnamed: 0", "search_keyword", "search_month"}

# Passthrough columns: raw header -> jsonl field.
PASSTHROUGH = {
    "pmid": "pmid",
    "doi": "doi",
    "title": "title",
    "abstract": "abstract",
    "summary": "summary",
    "authors": "authors",  # special-cased (python-repr list)
    "journal": "journal",
    "journal_abbr": "journal_abbr",
    "publication_date": "publication_date",
    "mesh_terms": "mesh_terms",  # special-cased (ElementTree repr)
    "publication_type": "publication_type",  # special-cased (ElementTree repr)
    "keywords": "keywords",  # special-cased (python-repr list)
    "country_data": "country_data",
    "country_author": "country_author",
    "validation_details": "validation_details",
    "deployment_details": "deployment_details",
    "trial_description": "trial_description",
    "metrics_reported": "metrics_reported",
    "F_SPECIFIC_MODELS_USED": "models_used",
}

AXIS_COLUMNS = {
    "A_CLINICAL_DOMAIN": "a_domain",
    "B_END_USER": "b_end_user",
    "C_CLINICAL_USE_CASE": "c_use_case",
    "D_ANALYTICS_TYPE": "d_analytics",
    "E_PRIMARY_FORMULATION": "e_formulation",
    "F_PRIMARY_ALGORITHM": "f_algorithm",
    "G_EVALUATION_TIER": "g_eval_tier",
}

FLAG_COLUMNS = {
    "internal_validation_conducted": "internal_val",
    "external_validation_conducted": "external_val",
    "temporal_validation_conducted": "temporal_val",
    "is_deployed_in_practice": "deployed",
    "randomized_trial_conducted": "rct",
    "confidence_intervals_reported": "ci_reported",
    "calibration_reported": "calibration_reported",
}

YEAR_COLUMNS = ("year", "search_year")

# Legacy presence flags: header -> tag name kept in ``tags``.
LEGACY_FLAGS = (
    ("patient", "patient"),
    ("intrinsic", "intrinsic"),
    ("red", "red"),
    ("field", "field"),
    ("clinician", "clinician"),
    ("operations", "operations"),
)

# Field order of every emitted JSON object (matches retrieval.sqlite_store.COLUMNS).
OUTPUT_FIELDS = (
    "pmid", "doi", "title", "abstract", "summary", "authors", "journal", "journal_abbr", "year",
    "publication_date", "mesh_names", "mesh_uis", "pub_types", "pub_type_uis", "keywords",
    "country_data", "country_author", "validation_details", "deployment_details",
    "trial_description", "metrics_reported", "models_used", "tags",
    "a_domain", "b_end_user", "c_use_case", "d_analytics", "e_formulation", "f_algorithm",
    "g_eval_tier",
    "internal_val", "external_val", "temporal_val", "deployed", "rct", "ci_reported",
    "calibration_reported",
    "search_text",
)

# Fields whose fill rate is reported after a build.
REPORT_FIELDS = (
    "pmid", "doi", "title", "abstract", "summary", "authors", "journal", "year",
    "publication_date", "mesh_names", "pub_types", "keywords", "country_data",
    "validation_details", "metrics_reported", "models_used",
    "a_domain", "b_end_user", "c_use_case", "d_analytics", "e_formulation", "f_algorithm",
    "g_eval_tier",
    "internal_val", "external_val", "temporal_val", "deployed", "rct", "ci_reported",
    "calibration_reported",
)

# ``ElementTree.tostring``-style reprs stored in mesh_terms / publication_type:
#   "<StringElement ('Sepsis', {'UI': 'D018805'}) at 0x...>"
# whitespace-tolerant: the repr may render as "StringElement('X', {...})" or
# "StringElement ('X', {'UI': 'D000001'})" depending on the element wrapper.
_STRING_ELEMENT = re.compile(r"StringElement\s*\(\s*'([^']+)'")
_UI = re.compile(r"'UI'\s*:\s*'([A-Za-z0-9]+)'")
# Fallback for python-repr lists that literal_eval rejects.
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


# --------------------------------------------------------------------------- #
# xlsx streaming
# --------------------------------------------------------------------------- #
def col_index(ref: str) -> int:
    """``"A1"`` -> 0, ``"AB12"`` -> 27. Returns -1 when the ref has no letters."""
    index = 0
    seen = False
    for char in ref:
        if char.isalpha():
            index = index * 26 + (ord(char.upper()) - 64)
            seen = True
        elif seen:
            break
    return index - 1 if seen else -1


class XlsxStream:
    """Streaming reader for a single-sheet xlsx (stdlib only)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def shared_strings(self) -> list[str]:
        """Inflate ``xl/sharedStrings.xml`` into an ordered list of strings."""
        strings: list[str] = []
        with zipfile.ZipFile(self.path) as archive:
            if SHARED_PART not in archive.namelist():
                return strings
            with archive.open(SHARED_PART) as handle:
                for _event, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag != f"{NS}si":
                        continue
                    # A shared string may be split over several <r><t> runs.
                    strings.append("".join(t.text or "" for t in elem.iter(f"{NS}t")))
                    elem.clear()
        return strings

    def rows(self) -> Iterator[dict[str, str]]:
        """Yield ``{header: value}`` dicts, one per data row."""
        strings = self.shared_strings()
        with zipfile.ZipFile(self.path) as archive:
            with archive.open(SHEET_PART) as handle:
                header: list[str] | None = None
                for _event, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag != f"{NS}row":
                        continue
                    cells = self._cells(elem, strings)
                    elem.clear()
                    width = (max(cells) + 1) if cells else 0
                    if header is None:
                        header = [(cells.get(i) or "").strip() for i in range(width)]
                        continue
                    if not cells:
                        continue
                    row = {
                        header[i]: cells.get(i, "")
                        for i in range(min(width, len(header)))
                        if header[i]
                    }
                    yield row

    @staticmethod
    def _cells(row_elem: ET.Element, strings: list[str]) -> dict[int, str]:
        cells: dict[int, str] = {}
        for cell in row_elem.iter(f"{NS}c"):
            index = col_index(cell.get("r") or "")
            if index < 0:
                index = len(cells)
            kind = cell.get("t")
            value = ""
            if kind == "inlineStr":
                inline = cell.find(f"{NS}is")
                if inline is not None:
                    value = "".join(t.text or "" for t in inline.iter(f"{NS}t"))
            else:
                node = cell.find(f"{NS}v")
                raw = (node.text or "") if node is not None else ""
                if kind == "s":
                    try:
                        value = strings[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                else:
                    value = raw
            cells[index] = value.strip()
        return cells


# --------------------------------------------------------------------------- #
# value normalisation
# --------------------------------------------------------------------------- #
def norm_pmid(value: str) -> str:
    """pmids are stored as floats in sci-notation: ``"1.99023E7"`` -> ``"19902300"``."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return text


def to_int(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


_TRUE = {"1", "1.0", "true", "yes", "y", "t"}
_FALSE = {"0", "0.0", "false", "no", "n", "f"}


def to_flag(value: str) -> int | None:
    """``'1'``/``'0'`` -> ``1``/``0``; anything else (incl. blank) -> ``None``."""
    text = (value or "").strip().lower()
    if text in _TRUE:
        return 1
    if text in _FALSE:
        return 0
    return None


def _truthy(value: str) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return False
    return text not in _FALSE


def parse_repr_list(value: str) -> list[str]:
    """Parse a python-repr list (``"['a', 'b']"``), falling back to quoted strings."""
    text = (value or "").strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        parsed = None
    if isinstance(parsed, (list, tuple, set)):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(parsed, str):
        return [parsed.strip()] if parsed.strip() else []
    out = []
    for single, double in _QUOTED.findall(text):
        item = (single or double).strip()
        if item:
            out.append(item)
    return out


def parse_element_reprs(value: str) -> tuple[list[str], list[str]]:
    """Extract names and UIs from serialised ``StringElement`` reprs."""
    text = value or ""
    if not text:
        return [], []
    names = [m.strip() for m in _STRING_ELEMENT.findall(text) if m.strip()]
    uis = [m.strip() for m in _UI.findall(text) if m.strip()]
    return names, uis


def build_search_text(row: dict[str, str]) -> str:
    parts = [
        row.get("title"),
        row.get("summary"),
        row.get("abstract"),
        *(row.get(key) for key in AXIS_COLUMNS.values()),
        row.get("models_used"),
        row.get("metrics_reported"),
        row.get("validation_details"),
        row.get("country_data"),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def convert_row(raw: dict[str, str]) -> dict[str, object]:
    """Raw xlsx row -> one JSONL record with the corpus field names."""
    out: dict[str, object] = dict.fromkeys(OUTPUT_FIELDS)

    for header, field in PASSTHROUGH.items():
        if header not in {"authors", "keywords", "mesh_terms", "publication_type"}:
            out[field] = (raw.get(header) or "").strip()

    out["pmid"] = norm_pmid(raw.get("pmid", ""))

    year = None
    for header in YEAR_COLUMNS:
        year = to_int(raw.get(header, ""))
        if year is not None:
            break
    out["year"] = year

    for header, field in AXIS_COLUMNS.items():
        out[field] = (raw.get(header) or "").strip()
    for header, field in FLAG_COLUMNS.items():
        out[field] = to_flag(raw.get(header, ""))

    mesh_names, mesh_uis = parse_element_reprs(raw.get("mesh_terms", ""))
    out["mesh_names"] = mesh_names
    out["mesh_uis"] = mesh_uis
    pub_types, pub_type_uis = parse_element_reprs(raw.get("publication_type", ""))
    out["pub_types"] = pub_types
    out["pub_type_uis"] = pub_type_uis

    out["authors"] = parse_repr_list(raw.get("authors", ""))
    out["keywords"] = parse_repr_list(raw.get("keywords", ""))

    tags = [tag for header, tag in LEGACY_FLAGS if _truthy(raw.get(header, ""))]
    out["tags"] = ", ".join(tags)

    out["search_text"] = build_search_text(out)  # type: ignore[arg-type]
    return out


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def _filled(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return str(value).strip() != ""


def fill_report(rows: list[dict]) -> dict[str, dict[str, float | int]]:
    total = len(rows)
    report: dict[str, dict[str, float | int]] = {}
    for field in REPORT_FIELDS:
        filled = sum(1 for row in rows if _filled(row.get(field)))
        report[field] = {"filled": filled, "pct": round(100.0 * filled / total, 1) if total else 0.0}
    return report


def build_jsonl(
    xlsx: Path,
    out: Path,
    *,
    limit: int | None = None,
    report_only: bool = False,
    force: bool = False,
) -> dict:
    """Stream the workbook into ``out`` (one JSON object per line).

    Dedupes on ``doi`` (first wins), drops rows without a pmid, never overwrites
    an existing JSONL unless ``force``. Returns the computed stats.
    """
    xlsx = Path(xlsx)
    if not xlsx.exists():
        raise FileNotFoundError(f"dataset not found: {xlsx}")
    return build_from_rows(
        XlsxStream(xlsx).rows(),
        out,
        limit=limit,
        report_only=report_only,
        force=force,
        source=str(xlsx),
    )


def build_from_rows(
    rows: Iterable[dict[str, str]],
    out: Path,
    *,
    limit: int | None = None,
    report_only: bool = False,
    force: bool = False,
    source: str = "",
) -> dict:
    """The xlsx-independent half of :func:`build_jsonl` (also the tested seam)."""
    out = Path(out)
    if out.exists() and not force and not report_only:
        raise FileExistsError(f"{out} exists; pass force=True (CLI: --force) to overwrite")

    stats: dict[str, object] = {
        "source": source,
        "out": str(out),
        "rows_read": 0,
        "blank_rows": 0,
        "written": 0,
        "skipped_no_pmid": 0,
        "skipped_duplicate_doi": 0,
        "limit": limit,
        "stopped_at_limit": False,
        "report_only": report_only,
    }
    kept: list[dict] = []
    seen_dois: set[str] = set()

    if not report_only:
        out.parent.mkdir(parents=True, exist_ok=True)
        handle = out.open("w", encoding="utf-8")
    else:
        handle = None

    try:
        for raw in rows:
            if not any((value or "").strip() for value in raw.values()):
                stats["blank_rows"] = int(stats["blank_rows"]) + 1
                continue
            stats["rows_read"] = int(stats["rows_read"]) + 1
            row = convert_row(raw)
            pmid = str(row.get("pmid") or "")
            if not pmid:
                stats["skipped_no_pmid"] = int(stats["skipped_no_pmid"]) + 1
                continue
            doi = str(row.get("doi") or "").strip().lower()
            if doi:
                if doi in seen_dois:
                    stats["skipped_duplicate_doi"] = int(stats["skipped_duplicate_doi"]) + 1
                    continue
                seen_dois.add(doi)
            kept.append(row)
            if handle is not None:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats["written"] = int(stats["written"]) + 1
            if limit is not None and int(stats["written"]) >= limit:
                stats["stopped_at_limit"] = True
                break
    finally:
        if handle is not None:
            handle.close()

    stats["fill_rates"] = fill_report(kept)
    stats["distinct_doi"] = len(seen_dois)

    _print_report(stats)
    if not report_only:
        report_path = out.with_suffix(".report.json")
        report_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        stats["report_path"] = str(report_path)
    return stats


def _print_report(stats: dict) -> None:
    print("=" * 68)
    print("xlsx -> jsonl build report")
    print("=" * 68)
    print(f"  source              : {stats.get('source') or '-'}")
    print(f"  rows read           : {stats['rows_read']}")
    print(f"  rows written        : {stats['written']}")
    print(f"  skipped (no pmid)   : {stats['skipped_no_pmid']}")
    print(f"  skipped (dup doi)   : {stats['skipped_duplicate_doi']}")
    print(f"  distinct dois       : {stats['distinct_doi']}")
    if stats.get("limit") is not None:
        print(f"  limit               : {stats['limit']} (reached: {stats['stopped_at_limit']})")
    if stats.get("report_only"):
        print("  mode                : report only (nothing written)")
    print("-" * 68)
    print("  fill rate per field")
    for field, info in stats.get("fill_rates", {}).items():
        print(f"    {field:<24} {info['filled']:>8}  {info['pct']:>5.1f}%")
    print("=" * 68)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def resolve_dataset(explicit: str | None) -> Path | None:
    """--xlsx, then $DATASET_PATH, then ./final_merged_dataset.xlsx."""
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.exists() else None
    env = os.environ.get("DATASET_PATH", "").strip()
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.exists() else None
    candidate = Path.cwd() / "final_merged_dataset.xlsx"
    return candidate if candidate.exists() else None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.ingest",
        description="Stream the xlsx corpus into a JSONL file (stdlib only).",
    )
    parser.add_argument("--xlsx", help="path to the dataset workbook (default: $DATASET_PATH or ./final_merged_dataset.xlsx)")
    parser.add_argument("--out", default="data/papers.jsonl", help="output JSONL (default: data/papers.jsonl)")
    parser.add_argument("--limit", type=int, default=None, help="stop after N written rows")
    parser.add_argument("--force", action="store_true", help="overwrite an existing JSONL")
    parser.add_argument("--report-only", action="store_true", help="measure and report, write nothing")
    args = parser.parse_args(argv)

    xlsx = resolve_dataset(args.xlsx)
    if xlsx is None:
        print("No dataset found.", file=sys.stderr)
        print("  looked at: --xlsx, $DATASET_PATH, ./final_merged_dataset.xlsx", file=sys.stderr)
        print("  set DATASET_PATH=/path/to/final_merged_dataset.xlsx (or pass --xlsx) and retry.", file=sys.stderr)
        return 2

    try:
        build_jsonl(
            xlsx,
            Path(args.out),
            limit=args.limit,
            report_only=args.report_only,
            force=args.force,
        )
    except FileExistsError as exc:
        print(f"refusing to overwrite: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    return 0


def main() -> None:  # pragma: no cover - CLI entry point
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()

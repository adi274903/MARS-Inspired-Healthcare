"""Ingest pipeline tests.

Fixture-driven only: everything here runs against ``tests/fixtures/sample_rows.jsonl``.
The dataset workbook is never opened, parsed or converted by the test suite - the
xlsx path is exercised by a human running ``python -m ingest.ingest``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingest import ingest as ing
from ingest import sqlite_build
from retrieval.sqlite_store import PaperStore
from retrieval.stats import corpus_overview
from tests.support import FIXTURE, fixture_rows

REPO_ROOT = Path(__file__).resolve().parent.parent

# Two rows of the fixture that mention sepsis (used for the FTS assertion).
SEPSIS_PMIDS = {"34152373", "31567890"}


# --------------------------------------------------------------------------- #
# raw-cell parsing (the shapes the real cells arrive in)
# --------------------------------------------------------------------------- #
MESH_CELL = (
    "<StringElement ('Sepsis', {'UI': 'D018805'})>, "
    "<StringElement ('Machine Learning', {'UI': 'D000069550'})>, "
    "<StringElement ('Validation Study', {'UI': 'D023361'})>"
)
PUBTYPE_CELL = (
    "<StringElement ('Journal Article', {'UI': 'D016428'})>, "
    "<StringElement ('Multicenter Study', {'UI': 'D016448'})>"
)


def test_pmid_float_scientific_notation_normalised():
    assert ing.norm_pmid("1.99023E7") == "19902300"
    assert ing.norm_pmid("34152373") == "34152373"
    assert ing.norm_pmid("34152373.0") == "34152373"
    assert ing.norm_pmid("") == ""
    # an unparseable value is passed through rather than lost
    assert ing.norm_pmid("not-a-pmid") == "not-a-pmid"


def test_authors_and_keywords_repr_lists_parsed():
    assert ing.parse_repr_list("['Ying Wang', 'Hong Wang', 'Hui Guo']") == [
        "Ying Wang",
        "Hong Wang",
        "Hui Guo",
    ]
    assert ing.parse_repr_list("[]") == []
    assert ing.parse_repr_list("") == []
    # literal_eval rejects an unbalanced list -> quoted-string fallback
    assert ing.parse_repr_list("['acute kidney injury', 'sepsis'") == ["acute kidney injury", "sepsis"]
    assert ing.parse_repr_list('["a", "b"]') == ["a", "b"]


def test_mesh_and_publication_type_element_reprs_extracted():
    names, uis = ing.parse_element_reprs(MESH_CELL)
    assert names == ["Sepsis", "Machine Learning", "Validation Study"]
    assert uis == ["D018805", "D000069550", "D023361"]

    types, type_uis = ing.parse_element_reprs(PUBTYPE_CELL)
    assert types == ["Journal Article", "Multicenter Study"]
    assert type_uis == ["D016428", "D016448"]

    assert ing.parse_element_reprs("") == ([], [])


def test_boolean_flag_coercion():
    assert ing.to_flag("1") == 1
    assert ing.to_flag("0") == 0
    assert ing.to_flag("") is None
    assert ing.to_flag("maybe") is None
    assert ing.to_flag("TRUE") == 1



def test_to_int_for_year():
    assert ing.to_int("2021") == 2021
    assert ing.to_int("2021.0") == 2021
    assert ing.to_int("") is None
    assert ing.to_int("n/a") is None


# --------------------------------------------------------------------------- #
# row conversion
# --------------------------------------------------------------------------- #
def _raw_row(**overrides: str) -> dict[str, str]:
    raw = {
        "pmid": "1.99023E7",
        "doi": "10.1007/s10278-009-9245-1",
        "title": "CAD on ultrasound image",
        "abstract": "Retrospective observer study.",
        "summary": "Fuzzy enhancement helps radiologists.",
        "authors": "['Ying Wang', 'Hong Wang']",
        "journal": "Journal of digital imaging",
        "journal_abbr": "Journal of digital i",
        "year": "2010",
        "publication_date": "2010",
        "mesh_terms": MESH_CELL,
        "publication_type": PUBTYPE_CELL,
        "keywords": "['ultrasound']",
        "country_data": "China",
        "country_author": "China",
        "validation_details": "Single-site retrospective",
        "deployment_details": "",
        "trial_description": "",
        "metrics_reported": "AUROC",
        "F_SPECIFIC_MODELS_USED": "fuzzy logic",
        "A_CLINICAL_DOMAIN": "Oncology",
        "B_END_USER": "Clinician",
        "C_CLINICAL_USE_CASE": "Diagnosis & triage",
        "D_ANALYTICS_TYPE": "Predictive",
        "E_PRIMARY_FORMULATION": "Binary classification",
        "F_PRIMARY_ALGORITHM": "Classical ML",
        "G_EVALUATION_TIER": "Internal split only",
        "internal_validation_conducted": "1",
        "external_validation_conducted": "0",
        "temporal_validation_conducted": "0",
        "is_deployed_in_practice": "0",
        "randomized_trial_conducted": "0",
        "confidence_intervals_reported": "1",
        "calibration_reported": "0",
        "clinician": "1",
        "patient": "0",
        "search_keyword": "",
        "search_month": "",
        "Unnamed: 0": "7",
    }
    raw.update(overrides)
    return raw


def test_convert_row_maps_and_cleans_fields():
    row = ing.convert_row(_raw_row())

    assert row["pmid"] == "19902300"
    assert row["year"] == 2010
    assert row["authors"] == ["Ying Wang", "Hong Wang"]
    assert row["keywords"] == ["ultrasound"]
    assert row["mesh_names"] == ["Sepsis", "Machine Learning", "Validation Study"]
    assert row["mesh_uis"][0] == "D018805"
    assert row["pub_types"] == ["Journal Article", "Multicenter Study"]
    assert row["internal_val"] == 1
    assert row["external_val"] == 0
    assert row["ci_reported"] == 1
    assert row["a_domain"] == "Oncology"
    assert row["g_eval_tier"] == "Internal split only"
    assert row["models_used"] == "fuzzy logic"
    assert row["tags"] == "clinician"
    # raw serialised columns never leak into the record
    assert "mesh_terms" not in row
    assert "publication_type" not in row
    assert "search_keyword" not in row
    assert "Unnamed: 0" not in row


def test_blank_flag_cells_become_none():
    row = ing.convert_row(_raw_row(external_validation_conducted="", calibration_reported=""))
    assert row["external_val"] is None
    assert row["calibration_reported"] is None


def test_search_text_assembly_order_and_contents():
    row = ing.convert_row(_raw_row())
    text = row["search_text"]

    expected_prelude = "CAD on ultrasound image Fuzzy enhancement helps radiologists. Retrospective observer study."
    assert text.startswith(expected_prelude)
    # all seven axes + models + metrics + validation_details + country_data
    for fragment in (
        "Oncology", "Clinician", "Diagnosis & triage", "Predictive",
        "Binary classification", "Classical ML", "Internal split only",
        "fuzzy logic", "AUROC", "Single-site retrospective", "China",
    ):
        assert fragment in text
    # the trailing order is fixed: metrics -> validation_details -> country_data
    assert text.endswith("AUROC Single-site retrospective China")


def test_duplicate_doi_first_wins_and_no_pmid_rows_dropped(tmp_path):
    rows = [
        _raw_row(pmid="1.99023E7", doi="10.1/abc"),
        _raw_row(pmid="2.0E7", doi="10.1/ABC"),  # same doi, different case -> dropped
        _raw_row(pmid="3.0E7", doi="10.1/def"),
        _raw_row(pmid="", doi="10.1/nopmid"),  # no pmid -> dropped
    ]
    out = tmp_path / "papers.jsonl"
    stats = ing.build_from_rows(rows, out, force=True)

    written = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["pmid"] for row in written] == ["19902300", "30000000"]
    assert stats["skipped_duplicate_doi"] == 1
    assert stats["skipped_no_pmid"] == 1
    assert stats["written"] == 2
    assert stats["distinct_doi"] == 2


def test_build_from_rows_refuses_overwrite_without_force(tmp_path):
    out = tmp_path / "papers.jsonl"
    out.write_text("existing\n")
    with pytest.raises(FileExistsError):
        ing.build_from_rows([_raw_row()], out)
    assert out.read_text() == "existing\n"


def test_cli_exits_2_with_no_dataset_path(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DATASET_PATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "ingest.ingest"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2, proc.stderr
    assert "dataset" in (proc.stderr + proc.stdout).lower()
    # nothing was created in the working directory
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------- #
# sqlite build against the fixture JSONL
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fixture_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "papers.jsonl"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


@pytest.fixture()
def built_db(fixture_jsonl: Path, tmp_path: Path) -> Path:
    db = tmp_path / "papers.db"
    stats = sqlite_build.build_db(fixture_jsonl, db)
    assert stats["papers"] == 12
    return db


def test_fixture_jsonl_has_12_rows():
    assert len(fixture_rows()) == 12


def test_build_db_creates_12_rows_and_working_fts(fixture_jsonl, tmp_path):
    db = tmp_path / "papers.db"
    stats = sqlite_build.build_db(fixture_jsonl, db)

    assert stats["read"] == 12
    assert stats["papers"] == 12
    assert stats["distinct_pmid"] == 12
    assert stats["fts_rows"] == 12
    assert stats["fts_query_ok"] is True
    assert stats["skipped_no_pmid"] == 0
    assert stats["skipped_duplicate_pmid"] == 0

    store = PaperStore(db)
    assert store.count() == 12

    # the FTS index is real: "sepsis" resolves to the sepsis papers
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT pmid FROM papers_fts WHERE papers_fts MATCH 'sepsis' ORDER BY pmid"
        ).fetchall()
    finally:
        conn.close()
    assert {row[0] for row in rows} == SEPSIS_PMIDS


def test_build_db_refuses_overwrite_without_force(fixture_jsonl, tmp_path):
    db = tmp_path / "papers.db"
    sqlite_build.build_db(fixture_jsonl, db)
    before = db.read_bytes()
    with pytest.raises(FileExistsError):
        sqlite_build.build_db(fixture_jsonl, db)
    assert db.read_bytes() == before
    # ... and rebuilds when forced
    stats = sqlite_build.build_db(fixture_jsonl, db, force=True)
    assert stats["papers"] == 12


def test_corpus_overview_totals(built_db):
    overview = corpus_overview(built_db)

    assert overview["total"] == 12
    assert overview["flags"]["external_val"] == 4
    assert overview["flags"]["deployed"] == 5
    assert overview["flags"]["rct"] == 1
    assert overview["year_range"] == [2010, 2023]
    assert set(overview["facets"]) == {
        "a_domain", "b_end_user", "c_use_case", "d_analytics",
        "e_formulation", "f_algorithm", "g_eval_tier",
    }
    domains = {row["key"]: row["n"] for row in overview["facets"]["a_domain"]}
    assert domains["Emergency & critical care"] == 4


def test_lists_are_stored_as_json(built_db):
    store = PaperStore(built_db)
    row = store.get_many(["34152373"])["34152373"]
    assert json.loads(row["authors"]) == ["Andrew Wong", "Alan Cao", "Jason Wang"]
    assert json.loads(row["mesh_names"]) == ["Sepsis", "Machine Learning", "Validation Study"]
    assert row["external_val"] == 1
    assert row["year"] == 2021

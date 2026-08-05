"""Live-source refresh scaffolding — exercised with a MOCK fetcher (never the network).

Pins: the source registry is well-formed (provenance + licence + parser present); a refresh
parses and loads under the pinned (source, version) with attribution surfaced; a dry run
inserts nothing; unknown sources fail closed; and the operator CLI refuses to fetch without an
explicit file/url.
"""
from pathlib import Path

import pytest

from app.models import EmissionFactor
from app.ef_catalog.sources import SOURCES
from app.ef_catalog.refresh import refresh_source

SAMPLES = Path("app/ef_catalog/samples")


def _defra_bytes(_url):
    # Mock fetcher: returns the bundled DEFRA sample, so no HTTP call is made.
    return (SAMPLES / "defra_flat_sample.csv").read_bytes()


def test_registry_entries_are_well_formed():
    assert SOURCES, "registry must not be empty"
    for key, s in SOURCES.items():
        assert s.key == key
        assert s.name and s.publisher and s.url
        assert s.license and s.attribution and s.default_source
        assert callable(s.parse)


def test_refresh_loads_factors_with_attribution(db):
    summary = refresh_source(db, "defra", "2024", fetcher=_defra_bytes)
    assert summary["loaded"] is True
    assert summary["source"] == "DEFRA_DESNZ"
    assert summary["version"] == "2024"
    assert summary["parsed_rows"] == 4          # same as parse_defra_flat_csv on the sample
    assert summary["added"] == 4
    assert "Open Government Licence" in summary["attribution"]
    assert db.query(EmissionFactor).filter(EmissionFactor.source == "DEFRA_DESNZ").count() == 4


def test_dry_run_inserts_nothing(db):
    summary = refresh_source(db, "defra", "2024", fetcher=_defra_bytes, dry_run=True)
    assert summary["dry_run"] is True and summary["loaded"] is False
    assert summary["parsed_rows"] == 4          # parsed, so counts are real
    assert db.query(EmissionFactor).count() == 0   # but nothing written


def test_refresh_supersedes_prior_version(db):
    refresh_source(db, "defra", "2024", fetcher=_defra_bytes)
    summary = refresh_source(db, "defra", "2025", fetcher=_defra_bytes)
    # every 2025 row supersedes its 2024 predecessor (same key, newer version)
    assert summary["superseded"] == 4
    assert db.query(EmissionFactor).count() == 8


def test_unknown_source_fails_closed(db):
    with pytest.raises(ValueError):
        refresh_source(db, "not_a_source", "1", fetcher=_defra_bytes)


def test_cli_refuses_without_file_or_url(capsys):
    from scripts.refresh_factors import main
    # No --file/--url: must refuse (return 2) and never fetch.
    rc = main(["--source", "defra", "--version", "2024"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to fetch" in err

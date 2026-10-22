"""Smoke test on the synthetic example: python tests/test_example.py (or pytest)."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from swmm_geomcheck import checks, inp  # noqa: E402
from swmm_geomcheck.__main__ import main  # noqa: E402

EX = ROOT / "examples" / "example_dual.inp"


def test_checks():
    R = checks.run(inp.load(EX)).R
    assert R["offsets_mode"] == "DEPTH"
    assert R["n_links_role"]["street"] == 40 and R["n_links_role"]["inlet"] == 4 and R["n_links_role"]["overflow"] == 5
    assert R["n_components"] == 2 and R["minor"]["n_gap_flag"] == 1          # raised isolated pipe Z0-Z1
    assert R["street_ends"]["n_mismatch"] == 0
    assert R["reversed"]["n"] == 1 and R["reversed"]["n_crest_above_storage"] == 0  # I1: DEPTH offset 1.5 = street
    assert R["cover"]["pipe"]["n_neg"] == 2                                    # P2 (0.3 m too tall), both ends
    assert R["n_links_with_vertices"] == 1


def test_cli():
    with tempfile.TemporaryDirectory() as d:
        assert main([str(EX), "-o", d]) == 0
        html = (Path(d) / "Geometry_consistency_report.html").read_text(encoding="utf-8")
        assert "Plotly.newPlot" in html and "[[FIG:" not in html


if __name__ == "__main__":
    test_checks()
    test_cli()
    print("ok")

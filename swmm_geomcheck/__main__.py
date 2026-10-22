"""Command line: python -m swmm_geomcheck MODEL.inp [-o OUTDIR] [options]"""
import argparse
import json
import sys
import time
import warnings
from pathlib import Path

from . import checks, inp, report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="swmm_geomcheck", description=(
        "Check the geometric consistency of a dual-drainage SWMM model (storage nodes = street surface, junctions = "
        "pipe/channel network) and write Geometry_consistency_report.html."))
    ap.add_argument("inp", type=Path, help="SWMM .inp file")
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output folder (default: <inp folder>/<inp name>_geometry_check)")
    ap.add_argument("--tin-max-edge", type=float, default=400.0,
                    help="drop surface triangles with an edge longer than this, in metres (default 400)")
    ap.add_argument("--surface-regex", default=None,
                    help="only STORAGE nodes whose name matches this regex are street surface (default: all)")
    ap.add_argument("--role", action="append", default=[], metavar="ROLE=REGEX",
                    help="force a link role by name, e.g. --role street=^street --role inlet=^gutter. "
                         f"Roles: {', '.join(checks.ROLES)}. Can be repeated.")
    ap.add_argument("--no-tables", action="store_true", help="do not write the CSV tables")
    a = ap.parse_args(argv)
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    if not a.inp.exists():
        ap.error(f"file not found: {a.inp}")
    out = a.out or a.inp.with_name(a.inp.stem + "_geometry_check")
    roles = {}
    for r in a.role:
        if "=" not in r:
            ap.error(f"--role expects ROLE=REGEX, got '{r}'")
        k, v = r.split("=", 1)
        roles[k.strip()] = v
    cfg = checks.Config(tin_max_edge=a.tin_max_edge, surface_regex=a.surface_regex, role_regex=roles)

    t0 = time.time()
    print(f"Reading {a.inp} ...", flush=True)
    model = inp.load(a.inp)
    print(f"  {len(model.nodes):,} nodes, {len(model.links):,} links, units {model.unit}, "
          f"LINK_OFFSETS={model.offsets_mode}", flush=True)
    for wmsg in model.warnings:
        print(f"  warning: {wmsg}")
    print("Running checks ...", flush=True)
    res = checks.run(model, cfg)
    print("Writing report ...", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    md = report.build_markdown(model, res)
    (out / "Geometry_consistency_report.md").write_text(report.md_for_file(md), encoding="utf-8")
    (out / "Geometry_consistency_report.html").write_text(
        report.build_html(md, res.figs, f"Geometry consistency – {model.path.name}"), encoding="utf-8")
    if not a.no_tables:
        (out / "tables").mkdir(exist_ok=True)
        for name, df in res.tables.items():
            df.to_csv(out / "tables" / f"{name}.csv", index=False)
    with open(out / "results.json", "w") as f:
        json.dump(res.R, f, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(f"Done in {time.time() - t0:.0f} s -> {out / 'Geometry_consistency_report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

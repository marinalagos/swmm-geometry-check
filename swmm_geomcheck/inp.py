"""Reader for the parts of a SWMM 5.x .inp file needed by the geometry checks.

All link offsets (conduit in/out offsets, orifice/weir/outlet crests) are converted to absolute elevations,
whatever the LINK_OFFSETS option (DEPTH, the SWMM default, or ELEVATION).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# SWMM cross-section shapes that are open at the top (everything else is closed)
OPEN_SHAPES = {"RECT_OPEN", "TRAPEZOIDAL", "TRIANGULAR", "PARABOLIC", "POWER", "IRREGULAR", "STREET"}
US_FLOW_UNITS = {"CFS", "GPM", "MGD"}


@dataclass
class Model:
    path: Path
    title: str
    options: dict
    nodes: pd.DataFrame      # index name: type, elev, maxdepth, x, y
    links: pd.DataFrame      # index name: ltype, from, to, length, off1, off2 (elevations), shape, g1..g4, transect, ...
    transects: dict          # name -> {lbank, rbank, sta[], elev[]}
    subcatchments: pd.DataFrame
    vertices: dict = field(default_factory=dict)  # link -> [(x, y), ...]
    warnings: list = field(default_factory=list)

    @property
    def unit(self):
        return "ft" if self.options.get("FLOW_UNITS", "CFS").upper() in US_FLOW_UNITS else "m"

    @property
    def offsets_mode(self):
        return self.options.get("LINK_OFFSETS", "DEPTH").upper()


def read_sections(path):
    sec = defaultdict(list)
    cur = None
    with open(path, encoding="latin-1") as f:
        for line in f:
            s = line.split(";", 1)[0].strip() if not line.lstrip().startswith(";") else ""
            if line.strip().startswith("["):
                cur = line.strip().strip("[]").upper()
                continue
            if s and cur:
                sec[cur].append(s.split())
    return sec


def _df(rows, cols):
    n = len(cols)
    return pd.DataFrame([list(r[:n]) + [None] * (n - len(r)) for r in rows], columns=cols)


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def parse_transects(rows):
    """HEC-2 style transects: X1 name nsta lbank rbank ...; GR elev station pairs."""
    tr, cur = {}, None
    for r in rows:
        if r[0] == "X1":
            cur = r[1]
            tr[cur] = {"lbank": float(r[3]) if len(r) > 3 else 0.0, "rbank": float(r[4]) if len(r) > 4 else 0.0,
                       "elev": [], "sta": []}
        elif r[0] == "GR" and cur is not None:
            v = list(map(float, r[1:]))
            tr[cur]["elev"] += v[0::2]
            tr[cur]["sta"] += v[1::2]
    return tr


def load(path):
    path = Path(path)
    sec = read_sections(path)
    warnings = []
    options = {r[0].upper(): " ".join(r[1:]) for r in sec.get("OPTIONS", []) if r}
    title = " ".join(" ".join(r) for r in sec.get("TITLE", [])[:1]) or path.stem

    # ---------------------------------------------------------------- nodes
    parts = []
    if sec.get("JUNCTIONS"):
        d = _df(sec["JUNCTIONS"], ["name", "elev", "maxdepth", "initdepth", "surdepth", "aponded"])
        d["type"] = "JUNCTION"
        parts.append(d)
    if sec.get("OUTFALLS"):
        d = _df(sec["OUTFALLS"], ["name", "elev", "otype"])
        d["type"] = "OUTFALL"
        parts.append(d)
    if sec.get("DIVIDERS"):
        d = _df(sec["DIVIDERS"], ["name", "elev"])
        d["type"] = "JUNCTION"  # a divider behaves as a junction geometrically
        parts.append(d)
    if sec.get("STORAGE"):
        d = _df(sec["STORAGE"], ["name", "elev", "maxdepth"])
        d["type"] = "STORAGE"
        parts.append(d)
    if not parts:
        raise ValueError("No nodes found in the .inp file")
    nodes = pd.concat(parts, ignore_index=True)
    for c in ["elev", "maxdepth", "surdepth", "aponded"]:
        if c in nodes:
            nodes[c] = _num(nodes[c])
    if sec.get("COORDINATES"):
        co = _df(sec["COORDINATES"], ["name", "x", "y"]).astype({"x": float, "y": float})
        nodes = nodes.merge(co.drop_duplicates("name"), on="name", how="left")
    else:
        nodes["x"] = nodes["y"] = np.nan
        warnings.append("No [COORDINATES] section: spatial checks (surface, maps, intersections) are skipped.")
    nodes = nodes.set_index("name")
    if nodes.index.duplicated().any():
        warnings.append(f"{nodes.index.duplicated().sum()} duplicated node names; the first definition is kept.")
        nodes = nodes[~nodes.index.duplicated()]
    n_nocoord = int(nodes.x.isna().sum())
    if n_nocoord and sec.get("COORDINATES"):
        warnings.append(f"{n_nocoord} nodes have no coordinates; they are left out of the spatial checks.")

    # ---------------------------------------------------------------- links
    parts = []
    if sec.get("CONDUITS"):
        d = _df(sec["CONDUITS"], ["name", "from", "to", "length", "rough", "off1_raw", "off2_raw"])
        d["ltype"] = "CONDUIT"
        parts.append(d)
    if sec.get("ORIFICES"):
        d = _df(sec["ORIFICES"], ["name", "from", "to", "otype", "off1_raw", "qcoeff", "gated"])
        d["ltype"] = "ORIFICE"
        parts.append(d)
    if sec.get("WEIRS"):
        d = _df(sec["WEIRS"], ["name", "from", "to", "wtype", "off1_raw", "qcoeff", "gated"])
        d["ltype"] = "WEIR"
        parts.append(d)
    if sec.get("OUTLETS"):
        d = _df(sec["OUTLETS"], ["name", "from", "to", "off1_raw"])
        d["ltype"] = "OUTLET"
        parts.append(d)
    if sec.get("PUMPS"):
        d = _df(sec["PUMPS"], ["name", "from", "to"])
        d["ltype"] = "PUMP"
        parts.append(d)
    links = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["name", "from", "to", "ltype"])
    for c in ["off1_raw", "off2_raw", "gated", "otype", "wtype", "length", "rough"]:
        if c not in links:
            links[c] = None
    links["length"] = _num(links["length"])
    links = links.set_index("name")
    bad = ~links["from"].isin(nodes.index) | ~links["to"].isin(nodes.index)
    if bad.any():
        warnings.append(f"{int(bad.sum())} links refer to undefined nodes and were dropped: "
                        + ", ".join(links.index[bad][:10]) + ("…" if bad.sum() > 10 else ""))
        links = links[~bad]

    # offsets -> absolute elevations
    mode = options.get("LINK_OFFSETS", "DEPTH").upper()
    e1 = links["from"].map(nodes.elev)
    e2 = links["to"].map(nodes.elev)

    def to_elev(raw, enode):
        star = raw.astype(str).str.strip() == "*"
        v = _num(raw.where(~star))
        if mode == "ELEVATION":
            out = v.where(~star, enode)
        else:
            out = enode + v.fillna(0).where(~star, 0)
        return out.where(raw.notna() | (mode != "ELEVATION"), enode)

    links["off1"] = to_elev(links["off1_raw"], e1)
    links["off2"] = to_elev(links["off2_raw"], e2)
    links.loc[links.ltype != "CONDUIT", "off2"] = np.nan
    links.loc[links.ltype == "PUMP", "off1"] = np.nan

    # cross sections
    if sec.get("XSECTIONS"):
        xs = _df(sec["XSECTIONS"], ["name", "shape", "g1", "g2", "g3", "g4", "barrels"]).drop_duplicates("name")
        links = links.join(xs.set_index("name"), how="left")
    else:
        for c in ["shape", "g1", "g2", "g3", "g4", "barrels"]:
            links[c] = None
    links["shape"] = links["shape"].fillna("").str.upper()
    irr = links["shape"] == "IRREGULAR"
    links["transect"] = links["g1"].where(irr)
    g1, g2 = _num(links["g1"].where(~irr)), _num(links["g2"].where(~irr))
    height = g1.copy()
    height[links["shape"] == "FILLED_CIRCULAR"] = (g1 - g2.fillna(0))[links["shape"] == "FILLED_CIRCULAR"]
    height[links["shape"] == "DUMMY"] = 0.0
    height[links["shape"].isin(["STREET", "IRREGULAR"])] = np.nan
    links["height"] = height
    links["open"] = links["shape"].isin(OPEN_SHAPES)

    transects = parse_transects(sec.get("TRANSECTS", []))
    missing_tr = set(links["transect"].dropna()) - set(transects)
    if missing_tr:
        warnings.append(f"{len(missing_tr)} transects referenced by links are not defined: {sorted(missing_tr)[:5]}")

    vertices = defaultdict(list)
    for r in sec.get("VERTICES", []):
        if len(r) >= 3 and r[0] in links.index:
            vertices[r[0]].append((float(r[1]), float(r[2])))

    subc = _df(sec.get("SUBCATCHMENTS", []), ["name", "gage", "outlet", "area"])
    subc["area"] = _num(subc["area"])
    return Model(path=path, title=title, options=options, nodes=nodes, links=links, transects=transects,
                 subcatchments=subc, vertices=dict(vertices), warnings=warnings)

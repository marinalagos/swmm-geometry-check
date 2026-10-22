"""Geometric consistency checks of a dual-drainage (surface + sub-surface) SWMM graph.

Link roles are assigned from the network topology and the cross-section shape, not from names:

    outfall_link  any link touching an OUTFALL node
    street        CONDUIT with at least one STORAGE end (surface flow)
    pipe          CONDUIT between non-storage nodes with a closed cross-section
    channel       CONDUIT between non-storage nodes with an open cross-section (IRREGULAR, RECT_OPEN, ...)
    inlet         ORIFICE between a STORAGE node and a JUNCTION (street -> sub-surface coupling)
    overflow      WEIR between a STORAGE node and a JUNCTION (street <-> channel coupling)
    other         everything else (pumps, outlets, structures between junctions, ...)

Roles can be forced with regular expressions on link names (Config.role_regex).
"""
import re
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import Delaunay, cKDTree

COUPLING_ROLES = ("inlet", "overflow")
ROLES = ("street", "pipe", "channel", "inlet", "overflow", "outfall_link", "other")


@dataclass
class Config:
    """Parameters. Lengths are given in metres and converted to feet for US-unit models."""
    tin_max_edge: float = 400.0          # drop surface triangles with a longer edge (no-street areas)
    tol: float = 0.001                   # elevation equality tolerance
    class_step: float = 10.0             # sampling step for the per-link classification
    piece: float = 1.0                   # piece length for the length-above-surface measure
    thresholds: tuple = (0, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
    gap_flag: float = 10.0               # disconnected component vs nearest main node: elevation gap to flag
    outlier: tuple = (1.0, 2.0, 5.0)     # storage invert vs median of street neighbours
    steep_pct: float = 5.0               # slope (%) listed as steep
    surface_regex: str = None            # only STORAGE nodes whose name matches are street surface
    role_regex: dict = field(default_factory=dict)  # {role: regex} forced roles

    def scaled(self, unit):
        if unit != "ft":
            return self
        f = 1 / 0.3048
        c = Config(**{**self.__dict__})
        c.tin_max_edge *= f; c.tol *= f; c.class_step *= f; c.piece *= f; c.gap_flag *= f
        c.thresholds = tuple(round(t * f, 2) for t in self.thresholds)
        c.outlier = tuple(round(t * f, 2) for t in self.outlier)
        return c


@dataclass
class Results:
    R: dict
    tables: dict
    figs: dict


# --------------------------------------------------------------------------------------------- helpers
def _stats(s, nd=3):
    s = pd.Series(s).dropna()
    if s.empty:
        return {}
    d = s.describe(percentiles=[.05, .25, .5, .75, .95])
    return {k: round(float(v), nd) for k, v in d.items()}


def bank_info(t):
    """Bank depth (above the thalweg) of the main channel of an HEC-2 transect.

    Outer near-vertical segments (|dx| <= 0.1, 'glass walls') are removed; the main channel is delimited by the
    X1 bank stations, or by the whole remaining profile when both are 0; the bank on each side is the highest
    point between the bank station and the thalweg; the channel depth is the lower of the two banks.
    """
    sta, el = np.array(t["sta"], float), np.array(t["elev"], float)
    if len(sta) < 3:
        return dict(lbank_sta=np.nan, rbank_sta=np.nan, bank_left=np.nan, bank_right=np.nan, bank_depth=np.nan,
                    full_depth=float(np.ptp(el)) if len(el) else np.nan, x1_banks_given=False)
    el = el - el.min()
    i0, i1 = 0, len(sta) - 1
    if sta[1] - sta[0] <= 0.1 and el[0] > el[1]:
        i0 = 1
    if sta[-1] - sta[-2] <= 0.1 and el[-1] > el[-2]:
        i1 -= 1
    s, e = sta[i0:i1 + 1], el[i0:i1 + 1]
    lb, rb = t["lbank"], t["rbank"]
    given = not (lb == 0 and rb == 0)
    if not given:
        lb, rb = s[0], s[-1]
    st = s[np.argmin(e)]
    main = (s >= lb - 1e-6) & (s <= rb + 1e-6)
    left, right = main & (s <= st), main & (s >= st)
    bl = e[left].max() if left.any() else np.nan
    br = e[right].max() if right.any() else np.nan
    return dict(lbank_sta=lb, rbank_sta=rb, bank_left=bl, bank_right=br, bank_depth=np.nanmin([bl, br]),
                full_depth=el.max(), x1_banks_given=given)


def segments(df, xa="x1", ya="y1", xb="x2", yb="y2", za=None, zb=None):
    n = len(df)
    xs = np.full(3 * n, np.nan); ys = xs.copy(); zs = xs.copy()
    xs[0::3], xs[1::3] = df[xa].values, df[xb].values
    ys[0::3], ys[1::3] = df[ya].values, df[yb].values
    if za is not None:
        zs[0::3], zs[1::3] = df[za].values, df[zb].values
    return xs, ys, zs


def _map_layout(fig, title, height=850):
    fig.update_layout(title=title, yaxis=dict(scaleanchor="x"), height=height, template="plotly_white",
                      legend=dict(orientation="h", y=-0.05), margin=dict(l=10, r=10, t=60, b=10))
    return fig


# --------------------------------------------------------------------------------------------- roles
def classify(model, role_regex=None):
    nodes, links = model.nodes, model.links
    T = nodes["type"]
    tf, tt = links["from"].map(T), links["to"].map(T)
    links["ft"] = tf.str[0] + tt.str[0]
    sto = (tf == "STORAGE") | (tt == "STORAGE")
    sj = ((tf == "STORAGE") & (tt == "JUNCTION")) | ((tf == "JUNCTION") & (tt == "STORAGE"))
    cond = links.ltype == "CONDUIT"
    role = pd.Series("other", index=links.index)
    role[cond & ~sto & links["open"]] = "channel"
    role[cond & ~sto & ~links["open"]] = "pipe"
    role[cond & sto] = "street"
    role[(links.ltype == "ORIFICE") & sj] = "inlet"
    role[(links.ltype == "WEIR") & sj] = "overflow"
    role[(tf == "OUTFALL") | (tt == "OUTFALL")] = "outfall_link"
    for r, rx in (role_regex or {}).items():
        if r not in ROLES:
            raise ValueError(f"unknown role '{r}' (valid: {', '.join(ROLES)})")
        role[links.index.str.match(rx)] = r
    links["role"] = role
    return links


# --------------------------------------------------------------------------------------------- main
def run(model, cfg=None):
    cfg = (cfg or Config()).scaled(model.unit)
    TOL = cfg.tol
    nodes, links = model.nodes, classify(model, cfg.role_regex)
    U = model.unit
    R, TAB, FIG = {}, {}, {}
    R["unit"] = U
    R["offsets_mode"] = model.offsets_mode
    R["flow_units"] = model.options.get("FLOW_UNITS", "CFS")
    R["routing"] = model.options.get("FLOW_ROUTING", "KINWAVE")
    R["warnings"] = list(model.warnings)
    R["cfg"] = {k: v for k, v in cfg.__dict__.items()}
    R["n_nodes"] = nodes["type"].value_counts().to_dict()
    R["n_links_type"] = links.ltype.value_counts().to_dict()
    R["n_links_role"] = links.role.value_counts().reindex(ROLES).fillna(0).astype(int).to_dict()
    TAB["link_roles"] = (links.groupby(["role", "ltype", "ft"]).size().rename("n").reset_index()
                         .assign(ft=lambda d: d.ft.map(lambda s: f"{s[0]}→{s[1]}")))
    shp = links[links.ltype == "CONDUIT"].groupby(["role", "shape"]).size().rename("n").reset_index()
    TAB["conduit_shapes"] = shp
    R["has_coords"] = bool(nodes.x.notna().any())

    for c in ["x", "y"]:
        links[f"{c}1"] = links["from"].map(nodes[c])
        links[f"{c}2"] = links["to"].map(nodes[c])
    links["e_from"] = links["from"].map(nodes.elev)
    links["e_to"] = links["to"].map(nodes.elev)
    links["dist"] = np.hypot(links.x2 - links.x1, links.y2 - links.y1)
    # plan length along vertices
    plen = links["dist"].copy()
    for name, vs in model.vertices.items():
        r = links.loc[name]
        pts = np.array([(r.x1, r.y1)] + vs + [(r.x2, r.y2)])
        plen[name] = np.hypot(*np.diff(pts, axis=0).T).sum()
    links["plan_len"] = plen
    R["n_links_with_vertices"] = len(model.vertices)

    # ------------------------------------------------------------------ transects / tops
    tdf = pd.DataFrame({k: bank_info(v) for k, v in model.transects.items()}).T
    if len(tdf):
        tdf.index.name = "transect"
        for r in ["channel", "street"]:
            tdf[f"n_{r}"] = links[links.role == r].transect.value_counts().reindex(tdf.index).fillna(0).astype(int)
        TAB["transect_banks"] = tdf.reset_index()
    links["top_depth"] = np.where(links.role == "pipe", links.height, np.nan)
    ch = links.role == "channel"
    links.loc[ch, "top_depth"] = links.loc[ch, "height"]
    chi = ch & (links["shape"] == "IRREGULAR")
    if len(tdf):
        links.loc[chi, "top_depth"] = links.loc[chi, "transect"].map(tdf.bank_depth).astype(float)
    links["top1"] = links.off1 + links.top_depth
    links["top2"] = links.off2 + links.top_depth
    R["pc_no_top"] = int(links[links.role.isin(["pipe", "channel"])].top_depth.isna().sum())

    # ------------------------------------------------------------------ 1. components
    G = nx.MultiGraph()
    G.add_nodes_from(nodes.index)
    G.add_edges_from(zip(links["from"], links["to"], links.index))
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    node_comp = {n: i for i, c in enumerate(comps) for n in c}
    nodes["comp"] = nodes.index.map(node_comp)
    links["comp"] = links["from"].map(node_comp)
    subc = model.subcatchments
    rows = []
    main_nodes = nodes[(nodes.comp == 0) & nodes.x.notna()]
    kd_main = cKDTree(main_nodes[["x", "y"]].values) if len(main_nodes) else None
    for i, c in enumerate(comps):
        nd, lk = nodes[nodes.comp == i], links[links.comp == i]
        row = dict(comp=i, n_nodes=len(nd), n_links=len(lk),
                   **{f"n_{t.lower()}": int((nd["type"] == t).sum()) for t in ["STORAGE", "JUNCTION", "OUTFALL"]},
                   **{f"n_{r}": int((lk.role == r).sum()) for r in ROLES},
                   elev_min=nd.elev.min(), elev_max=nd.elev.max(),
                   n_subcatch=int(subc.outlet.isin(c).sum()), subcatch_area=float(subc.area[subc.outlet.isin(c)].sum()),
                   nodes=" ".join(sorted(nd.index)) if len(nd) <= 8 else f"{len(nd)} nodes")
        if i > 0 and kd_main is not None and nd.x.notna().any():
            ndx = nd[nd.x.notna()]
            d, j = kd_main.query(ndx[["x", "y"]].values)
            k = int(np.argmin(d))
            row.update(dist_to_main=float(d[k]), nearest_main_node=main_nodes.index[j[k]],
                       nearest_main_type=main_nodes["type"].iloc[j[k]], elev_nearest_main=float(main_nodes.elev.iloc[j[k]]),
                       elev_own=float(ndx.elev.iloc[k]), gap=float(ndx.elev.iloc[k] - main_nodes.elev.iloc[j[k]]))
        rows.append(row)
    cdf = pd.DataFrame(rows)
    TAB["components"] = cdf
    R["n_components"] = len(comps)
    R["main"] = dict(n_nodes=int(cdf.n_nodes[0]), n_links=int(cdf.n_links[0]), n_outfall=int(cdf.n_outfall[0]))
    minor = cdf.iloc[1:]
    R["minor"] = dict(n=len(minor), n_nodes=int(minor.n_nodes.sum()), n_links=int(minor.n_links.sum()),
                      n_with_outfall=int((minor.n_outfall > 0).sum()), n_subcatch=int(minor.n_subcatch.sum()),
                      subcatch_area=round(float(minor.subcatch_area.sum()), 3),
                      main_subcatch_area=round(float(cdf.subcatch_area[0]), 3),
                      n_gap_flag=int((minor.get("gap", pd.Series(dtype=float)).abs() > cfg.gap_flag).sum()),
                      gap_median=float(minor["gap"].median()) if "gap" in minor and minor["gap"].notna().any() else None)

    def layer_components(roles, types):
        g = nx.Graph()
        g.add_nodes_from(nodes.index[nodes["type"].isin(types)])
        lk = links[links.role.isin(roles) & links["from"].isin(g) & links["to"].isin(g)]
        g.add_edges_from(zip(lk["from"], lk["to"]))
        return sorted(nx.connected_components(g), key=len, reverse=True)

    surf_cs = layer_components(["street"], ["STORAGE"])
    sub_cs = layer_components(["pipe", "channel", "outfall_link", "other"], ["JUNCTION", "OUTFALL"])
    outf = set(nodes.index[nodes["type"] == "OUTFALL"])
    R["surface_layer"] = dict(n=len(surf_cs), sizes=[len(c) for c in surf_cs[:8]], singletons=sum(len(c) == 1 for c in surf_cs))
    sub_rows = [dict(n_nodes=len(c), has_outfall=bool(c & outf),
                     n_pipe=int(links[(links.role == "pipe") & links["from"].isin(c)].shape[0]),
                     n_channel=int(links[(links.role == "channel") & links["from"].isin(c)].shape[0])) for c in sub_cs]
    sub_df = pd.DataFrame(sub_rows)
    R["subsurface_layer"] = dict(n=len(sub_cs), sizes=[len(c) for c in sub_cs[:8]],
                                 singletons=sum(len(c) == 1 for c in sub_cs),
                                 n_no_outfall=int((~sub_df.has_outfall & (sub_df.n_nodes > 1)).sum()) if len(sub_df) else 0,
                                 nodes_no_outfall=int(sub_df[~sub_df.has_outfall & (sub_df.n_nodes > 1)].n_nodes.sum()) if len(sub_df) else 0,
                                 pipes_no_outfall=int(sub_df[~sub_df.has_outfall].n_pipe.sum()) if len(sub_df) else 0,
                                 channels_no_outfall=int(sub_df[~sub_df.has_outfall].n_channel.sum()) if len(sub_df) else 0)

    if R["has_coords"]:
        fig = go.Figure()
        x, y, _ = segments(links[links.comp == 0])
        fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", line=dict(color="lightgrey", width=1), name="main component"))
        pal = px.colors.qualitative.Bold
        for i in cdf.index[1:61]:
            lk, nd = links[links.comp == i], nodes[nodes.comp == i]
            col = pal[(i - 1) % len(pal)]
            if len(lk):
                x, y, _ = segments(lk)
                fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", line=dict(color=col, width=3), legendgroup=str(i),
                                           showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scattergl(x=nd.x, y=nd.y, mode="markers", marker=dict(color=col, size=8), legendgroup=str(i),
                                       name=f"comp {i}: {len(nd)} nodes / {len(lk)} links",
                                       text=[f"{n}<br>{t}<br>elev {e:.2f}" for n, t, e in zip(nd.index, nd["type"], nd.elev)],
                                       hoverinfo="text"))
        FIG["components_map"] = _map_layout(fig, "Connected components (grey: main component)")

    # couplings: (coupling link, junction, storage)
    coup = links[links.role.isin(COUPLING_ROLES)]
    cp = pd.DataFrame({"j": np.where(coup["from"].map(nodes["type"]) == "STORAGE", coup["to"], coup["from"]),
                       "s": np.where(coup["from"].map(nodes["type"]) == "STORAGE", coup["from"], coup["to"]),
                       "role": coup.role.values}, index=coup.index)
    pcl = links[links.role.isin(["pipe", "channel"])]
    jnet = pd.concat([pcl[["from", "role"]].rename(columns={"from": "j"}), pcl[["to", "role"]].rename(columns={"to": "j"})])
    jkind = jnet.groupby("j").role.agg(lambda s: "+".join(sorted(set(s))))

    # ------------------------------------------------------------------ 2. storage<->junction links that are not couplings
    nsj = links[links.ft.isin(["SJ", "JS"]) & ~links.role.isin(COUPLING_ROLES)].copy()
    if len(nsj):
        nsj["junction"] = np.where(nsj.ft == "JS", nsj["from"], nsj["to"])
        nsj["storage"] = np.where(nsj.ft == "JS", nsj["to"], nsj["from"])
        nsj["junction_net"] = nsj.junction.map(jkind).fillna("none")
        nsj["elev_junction"] = nsj.junction.map(nodes.elev)
        nsj["elev_storage"] = nsj.storage.map(nodes.elev)
        nsj["inv_junction_end"] = np.where(nsj.ft == "JS", nsj.off1, nsj.off2)
        nsj["inv_storage_end"] = np.where(nsj.ft == "JS", nsj.off2, nsj.off1)
        nsj["inv_j_end_minus_junction"] = nsj.inv_junction_end - nsj.elev_junction
        nsj["inv_s_end_minus_storage"] = nsj.inv_storage_end - nsj.elev_storage
        nsj["storage_minus_inv_j_end"] = nsj.elev_storage - nsj.inv_junction_end
        nsj["slope_pct"] = 100 * (nsj.inv_storage_end - nsj.inv_junction_end) / nsj.length
        nsj["n_couplings_at_junction"] = nsj.junction.map(cp.j.value_counts()).fillna(0).astype(int)
        nsj["direction"] = np.where(nsj.ft == "JS", "junction→storage", "storage→junction")
        cols = ["role", "ltype", "direction", "junction", "junction_net", "storage", "shape", "transect", "length",
                "elev_storage", "inv_storage_end", "elev_junction", "inv_junction_end", "inv_j_end_minus_junction",
                "storage_minus_inv_j_end", "inv_s_end_minus_storage", "slope_pct", "n_couplings_at_junction"]
        TAB["storage_junction_noncoupling"] = nsj[cols].reset_index().round(3)
        multi = nsj.junction.value_counts()
        R["nsj"] = dict(n=len(nsj), by_role=nsj.role.value_counts().to_dict(),
                        by_direction=nsj.direction.value_counts().to_dict(), n_junctions=int(nsj.junction.nunique()),
                        junction_net=nsj.junction_net.value_counts().to_dict(),
                        junctions_multi=int((multi > 1).sum()),
                        s_end_match=int((nsj.inv_s_end_minus_storage.abs() <= TOL).sum()),
                        j_end_above_junction=_stats(nsj.inv_j_end_minus_junction),
                        storage_minus_j_end=_stats(nsj.storage_minus_inv_j_end),
                        slope=_stats(nsj.slope_pct))
    else:
        R["nsj"] = dict(n=0)

    # ------------------------------------------------------------------ 3. storage vs street ends, surface
    st_l = links[links.role == "street"]
    ends = pd.concat([pd.DataFrame({"node": st_l["from"], "link": st_l.index, "end": 1, "inv": st_l.off1}),
                      pd.DataFrame({"node": st_l["to"], "link": st_l.index, "end": 2, "inv": st_l.off2})])
    ends = ends[ends.node.map(nodes["type"]) == "STORAGE"]
    ends["node_elev"] = ends.node.map(nodes.elev)
    ends["diff"] = ends.inv - ends.node_elev
    mism = ends[ends["diff"].abs() > TOL]
    TAB["street_end_mismatch"] = mism.round(3)
    storage = nodes[nodes["type"] == "STORAGE"]
    no_street = storage.index[~storage.index.isin(ends.node)]
    TAB["storage_without_street"] = storage.loc[no_street, ["elev", "comp"]].reset_index()
    R["street_ends"] = dict(n=len(ends), n_mismatch=len(mism), max_abs=float(ends["diff"].abs().max()) if len(ends) else None,
                            n_storage_nodes_mismatch=int(mism.node.nunique()), mismatch=_stats(mism["diff"]),
                            storage_without_street=len(no_street))

    surf = storage[(storage.comp == 0) & storage.x.notna()]
    if cfg.surface_regex:
        surf = surf[surf.index.str.match(cfg.surface_regex)]
    R["surface_ok"] = len(surf) >= 3
    R["n_surface_nodes"] = len(surf)
    R["surface_excluded_minor"] = int(((storage.comp != 0)).sum())
    if R["surface_ok"]:
        P, Z = surf[["x", "y"]].values, surf.elev.values
        tri = Delaunay(P)
        smp = tri.simplices
        el = np.max(np.stack([np.linalg.norm(P[smp[:, a]] - P[smp[:, b]], axis=1) for a, b in [(0, 1), (1, 2), (0, 2)]]), 0)
        keep = el <= cfg.tin_max_edge
        a = P[smp]
        area = 0.5 * np.abs((a[:, 1, 0] - a[:, 0, 0]) * (a[:, 2, 1] - a[:, 0, 1]) - (a[:, 2, 0] - a[:, 0, 0]) * (a[:, 1, 1] - a[:, 0, 1]))
        interp = LinearNDInterpolator(tri, Z)
        R["tin"] = dict(n_tri=len(smp), n_keep=int(keep.sum()), area_keep_km2=round(float(area[keep].sum()) / 1e6, 2),
                        area_drop_km2=round(float(area[~keep].sum()) / 1e6, 2), median_edge=round(float(np.median(el)), 1),
                        elev=_stats(Z))

        def surface_z(x, y):
            pts = np.column_stack([x, y])
            z = interp(pts)
            simp = tri.find_simplex(pts)
            z[(simp < 0) | ~keep[np.maximum(simp, 0)]] = np.nan
            return z

        x0, y0 = P.min(0)
        zlo, zhi = np.percentile(Z, [0.5, 99.5])
        tri_keep = smp[keep]

        def mesh(opacity=1.0):
            return go.Mesh3d(x=P[:, 0] - x0, y=P[:, 1] - y0, z=Z, i=tri_keep[:, 0], j=tri_keep[:, 1], k=tri_keep[:, 2],
                             intensity=Z, colorscale="earth", cmin=zlo, cmax=zhi, opacity=opacity, showlegend=True,
                             name="street surface (TIN)", colorbar=dict(title=f"elev ({U})"), hoverinfo="z")

        def layout3d(fig, title):
            ar = np.ptp(P[:, 0]) / max(np.ptp(P[:, 1]), 1e-9)
            asp = dict(x=1, y=1 / ar, z=0.25) if ar >= 1 else dict(x=ar, y=1, z=0.25)
            fig.update_layout(title=title, height=850, legend=dict(x=0, y=1), margin=dict(l=0, r=0, t=50, b=0),
                              scene=dict(aspectmode="manual", aspectratio=asp,
                                         xaxis_title=f"x − {x0:.0f} ({U})", yaxis_title=f"y − {y0:.0f} ({U})",
                                         zaxis_title=f"elev ({U})"))
            return fig

        FIG["surface_3d"] = layout3d(go.Figure(mesh()), "Street surface: Delaunay TIN of storage-node inverts")
        fig = go.Figure(go.Scattergl(x=P[:, 0], y=P[:, 1], mode="markers",
                                     marker=dict(color=Z, colorscale="earth", cmin=zlo, cmax=zhi, size=3,
                                                 colorbar=dict(title=f"elev ({U})")),
                                     text=[f"{n}<br>elev {e:.3f}" for n, e in zip(surf.index, Z)], hoverinfo="text"))
        FIG["surface_map"] = _map_layout(fig, "Storage-node inverts (street surface)")

    # ------------------------------------------------------------------ 4. reversed couplings
    cpl = coup.copy()
    cpl["junction"], cpl["storage"] = cp.j, cp.s
    cpl["crest"] = cpl.off1
    cpl["elev_storage"] = cpl.storage.map(nodes.elev)
    cpl["elev_junction"] = cpl.junction.map(nodes.elev)
    cpl["crest_minus_storage"] = cpl.crest - cpl.elev_storage
    cpl["crest_minus_junction"] = cpl.crest - cpl.elev_junction
    cpl["reversed"] = cpl["from"] == cpl.junction
    R["couplings"] = {}
    for r in COUPLING_ROLES:
        d = cpl[cpl.role == r]
        nrm = d[~d.reversed]
        R["couplings"][r] = dict(n=len(d), n_reversed=int(d.reversed.sum()),
                                 gated=d.gated.astype(str).str.upper().eq("YES").sum().item() if len(d) else 0,
                                 crest_minus_storage=_stats(nrm.crest_minus_storage),
                                 crest_eq_storage=int((nrm.crest_minus_storage.abs() <= TOL).sum()),
                                 crest_minus_storage_mode=float(nrm.crest_minus_storage.round(3).mode().iloc[0]) if len(nrm) else None,
                                 crest_minus_junction=_stats(d.crest_minus_junction),
                                 dist=_stats(d.dist, 1), types=d.otype.fillna(d.wtype).value_counts().to_dict())
    rev = cpl[cpl.reversed].copy()
    if len(rev):
        sib = cpl[~cpl.reversed]

        def sibling_info(r):
            s = sib[sib.junction == r.junction]
            match = s.index[(s.crest - r.crest).abs() <= TOL].tolist()
            return pd.Series(dict(n_siblings=len(s), crest_equals_sibling=" ".join(match)))

        rev = rev.join(rev.apply(sibling_info, axis=1))
        rev["storage_below_junction_invert"] = rev.elev_storage < rev.elev_junction - TOL
        rev["junction_net"] = rev.junction.map(jkind).fillna("none")
        TAB["reversed_couplings"] = rev[["role", "ltype", "from", "to", "junction", "junction_net", "storage", "crest",
                                         "elev_junction", "elev_storage", "crest_minus_storage", "crest_minus_junction",
                                         "dist", "gated", "n_siblings", "crest_equals_sibling",
                                         "storage_below_junction_invert"]].reset_index().round(3)
    R["reversed"] = dict(n=len(rev), by_role=rev.role.value_counts().to_dict() if len(rev) else {},
                         crest_minus_storage=_stats(rev.crest_minus_storage) if len(rev) else {},
                         n_crest_above_storage=int((rev.crest_minus_storage > TOL).sum()) if len(rev) else 0,
                         n_crest_eq_sibling=int((rev.crest_equals_sibling != "").sum()) if len(rev) else 0,
                         n_storage_below_junction=int(rev.storage_below_junction_invert.sum()) if len(rev) else 0)

    # ------------------------------------------------------------------ 5. per-connection crown/bank vs storage
    pc = links[links.role.isin(["pipe", "channel"]) & (links.comp == 0)]
    E = pd.concat([pd.DataFrame({"link": pc.index, "kind": pc.role.values, "end": e, "junction": pc[nd].values,
                                 "invert": pc[inv].values, "top": pc[top].values, "top_depth": pc.top_depth.values,
                                 "shape": pc["shape"].values, "x": pc[f"x{e}"].values, "y": pc[f"y{e}"].values})
                   for e, nd, inv, top in [(1, "from", "off1", "top1"), (2, "to", "off2", "top2")]], ignore_index=True)
    E = E[E.junction.map(nodes["type"]) != "STORAGE"]
    C = E.merge(cp.rename_axis("coupling").reset_index().rename(columns={"role": "coupling_role", "s": "storage"}),
                left_on="junction", right_on="j", how="inner").drop(columns="j")
    C["crest"] = C.coupling.map(links.off1)
    C["storage_elev"] = C.storage.map(nodes.elev)
    C["coupling_dist"] = C.coupling.map(links.dist)
    C["cover"] = C.storage_elev - C.top
    C["n_conn_at_end"] = C.groupby(["link", "end"]).coupling.transform("size")
    C["spread_at_end"] = C.groupby(["link", "end"]).storage_elev.transform(lambda s: s.max() - s.min())
    C = C[C.top.notna()].sort_values(["kind", "link", "end", "coupling"])
    TAB["connections"] = C[["link", "kind", "end", "junction", "shape", "top_depth", "invert", "top", "coupling",
                            "coupling_role", "storage", "storage_elev", "crest", "cover", "coupling_dist",
                            "n_conn_at_end", "spread_at_end"]].round(3)
    R["cover"] = {}
    for k in ["pipe", "channel"]:
        d = C[C.kind == k]
        e_k = E[E.kind == k]
        n_links_k = int((pc.role == k).sum())
        if not len(d):
            R["cover"][k] = dict(n_conn=0, total_links=n_links_k, total_ends=len(e_k))
            continue
        ends_k = d.drop_duplicates(["link", "end"])
        g = d.groupby(["link", "end"]).cover.agg(["min", "max"])
        c = d.cover
        R["cover"][k] = dict(
            n_conn=len(d), n_ends=len(ends_k), total_ends=len(e_k), n_links=int(d.link.nunique()), total_links=n_links_k,
            by_coupling=d.coupling_role.value_counts().to_dict(),
            conn_per_end=ends_k.n_conn_at_end.value_counts().sort_index().to_dict(),
            spread_gt=int((ends_k.spread_at_end > 0.5 * (1 if U == "m" else 1 / 0.3048)).sum()),
            spread_max=round(float(ends_k.spread_at_end.max()), 3),
            stats=_stats(c), n_neg=int((c < -TOL).sum()), n_neg_05=int((c < -cfg.thresholds[4]).sum()),
            n_neg_1=int((c < -cfg.thresholds[6]).sum()), n_zero=int((c.abs() <= cfg.thresholds[1]).sum()),
            pct_neg=round(100 * float((c < -TOL).mean()), 1),
            ends_any_neg=int((g["min"] < -TOL).sum()), ends_all_neg=int((g["max"] < -TOL).sum()),
            ends_mixed=int(((g["min"] < -TOL) & (g["max"] > TOL)).sum()), links_any_neg=int(d[c < -TOL].link.nunique()),
            by_coupling_stats={cr: dict(n=len(gg), median=round(float(gg.cover.median()), 3), n_neg=int((gg.cover < -TOL).sum()))
                               for cr, gg in d.groupby("coupling_role")},
            worst=d.nsmallest(8, "cover")[["link", "end", "junction", "shape", "top_depth", "top", "coupling", "storage",
                                           "storage_elev", "cover"]].round(3).to_dict("records"))
        if k == "pipe":
            neg = d[c < -TOL]
            R["cover"][k]["neg_shallow_manhole"] = int((neg.top_depth > neg.storage_elev - neg.junction.map(nodes.elev)).sum())
            bs = d.assign(size=d["shape"] + " " + d.top_depth.round(2).astype(str)).groupby("size").cover.agg(
                n="size", n_neg=lambda s: int((s < -TOL).sum()), median="median")
            R["cover"][k]["by_size"] = bs[bs.n_neg > 0].sort_values("n_neg", ascending=False).head(8).round(3).reset_index().to_dict("records")
    R["n_conn_total"] = len(C)
    if len(C):
        FIG["cover_hist"] = px.histogram(C, x="cover", color="coupling_role", facet_row="kind", nbins=200, barmode="overlay",
                                         template="plotly_white", height=650,
                                         title=f"Storage invert − pipe crown / channel bank per connection ({U}); < 0: top above street"
                                         ).update_xaxes(range=[float(C.cover.quantile(.005)) - 0.5, float(C.cover.quantile(.995)) + 0.5])
        if R["has_coords"]:
            cc = C.assign(sx=C.storage.map(nodes.x), sy=C.storage.map(nodes.y))
            lim = max(1.0, float(cc.cover.abs().quantile(.98)))
            fig = go.Figure()
            x, y, _ = segments(links[links.role == "street"])
            fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", line=dict(color="#dddddd", width=1), name="streets"))
            xs, ys, _ = segments(cc, xa="x", ya="y", xb="sx", yb="sy")
            fig.add_trace(go.Scattergl(x=xs, y=ys, mode="lines", line=dict(color="#999", width=1), name="inlet/overflow"))
            for k, sym in [("pipe", "circle"), ("channel", "diamond")]:
                d = cc[cc.kind == k]
                fig.add_trace(go.Scattergl(
                    x=d.sx, y=d.sy, mode="markers", name=f"{k} connections", hoverinfo="text",
                    marker=dict(color=d.cover.clip(-lim, lim), colorscale="RdBu", cmin=-lim, cmax=lim, size=6, symbol=sym,
                                colorbar=dict(title=f"storage − top ({U})")),
                    text=[f"{l} end{e} @ {j}<br>top {t:.2f}<br>{g} → {s} (elev {se:.2f})<br>cover {c:.2f}"
                          for l, e, j, t, g, s, se, c in zip(d.link, d.end, d.junction, d.top, d.coupling, d.storage,
                                                             d.storage_elev, d.cover)]))
            FIG["cover_map"] = _map_layout(fig, "Cover (storage invert − crown/bank) per connection, drawn at the storage node. Red: top above street")

    # ------------------------------------------------------------------ 6/7. 3D plot, intersections
    if R["surface_ok"]:
        fig = go.Figure(mesh(opacity=0.85))
        for k, col, nm in [("pipe", "#d62728", "pipe crowns"), ("channel", "#1f3fff", "channel banks")]:
            d = links[(links.role == k) & (links.comp == 0) & links.top1.notna()]
            if len(d):
                xs, ys, zs = segments(d, za="top1", zb="top2")
                fig.add_trace(go.Scatter3d(x=xs - x0, y=ys - y0, z=zs, mode="lines", line=dict(color=col, width=4),
                                           name=nm, text=np.repeat(d.index.values, 3), hoverinfo="text+z"))
        FIG["crowns_banks_3d"] = layout3d(fig, "Street surface (TIN) with pipe crowns (red) and channel banks (blue)")

        pcs = pc[pc.top1.notna() & pc.x1.notna() & pc.x2.notna()]

        def link_points(name, r, step, midpoints):
            pts = np.array([(r.x1, r.y1)] + model.vertices.get(name, []) + [(r.x2, r.y2)])
            seg = np.hypot(*np.diff(pts, axis=0).T)
            cum = np.concatenate([[0], np.cumsum(seg)])
            L = cum[-1]
            n = max(int(np.ceil(L / step)), 1)
            s = (np.arange(n) + 0.5) / n * L if midpoints else np.linspace(0, L, n + 1)
            xs = np.interp(s, cum, pts[:, 0]); ys = np.interp(s, cum, pts[:, 1])
            t = s / L if L > 0 else np.zeros_like(s)
            return xs, ys, r.top1 + t * (r.top2 - r.top1), len(s)

        # per-link classification (end-inclusive points every <= class_step)
        X, Y, ZT, owner = [], [], [], []
        for i, (name, r) in enumerate(pcs.iterrows()):
            xs, ys, zt, n = link_points(name, r, cfg.class_step, False)
            X.append(xs); Y.append(ys); ZT.append(zt); owner.append(np.full(n, i))
        if X:
            X, Y, ZT, owner = map(np.concatenate, (X, Y, ZT, owner))
            ex = ZT - surface_z(X, Y)  # >0 : top above surface
            df = pd.DataFrame({"i": owner, "e": ex})
            ok = df.dropna()
            agg = ok.groupby("i").e.agg(n_assessed="size", max_exceed="max", min_exceed="min",
                                         frac_above=lambda e: float((e > TOL).mean()))

            def ncross(e):
                e = e[np.abs(e) > TOL]
                return int(np.sum(np.diff(np.sign(e)) != 0))
            agg["n_crossings"] = ok.groupby("i").e.apply(lambda s: ncross(s.values))
            S = pd.DataFrame({"link": pcs.index, "kind": pcs.role.values, "length": pcs.length.values,
                              "n_samples": df.groupby("i").size().values}).join(agg)
            S["n_assessed"] = S.n_assessed.fillna(0).astype(int)
            S["class"] = np.select([S.n_assessed == 0, S.frac_above == 0, S.frac_above == 1],
                                   ["outside surface", "below surface", "entirely above surface"], "crosses surface")
            edges = list(cfg.thresholds[3:])
            lab = (["none", f"0–{edges[0]:g}"] + [f"{lo:g}–{hi:g}" for lo, hi in zip(edges[:-1], edges[1:])]
                   + [f"> {edges[-1]:g}"])
            S["severity"] = pd.cut(S.max_exceed, [-np.inf, TOL] + edges + [np.inf], labels=lab)
            TAB["intersections"] = S.round(3)
            R["intersections"] = {k: dict(classes=S[S.kind == k]["class"].value_counts().to_dict(),
                                          severity={str(a): int(v) for a, v in S[S.kind == k].severity.value_counts().sort_index().items()})
                                  for k in ["pipe", "channel"]}
            if R["has_coords"]:
                fig = go.Figure()
                x, y, _ = segments(links[links.role == "street"])
                fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", line=dict(color="#e5e5e5", width=1), name="streets"))
                for cl, col in [("below surface", "#9ecae1"), ("crosses surface", "#fd8d3c"),
                                ("entirely above surface", "#d62728"), ("outside surface", "#636363")]:
                    for k, dash in [("pipe", "solid"), ("channel", "dot")]:
                        ids = S.link[(S["class"] == cl) & (S.kind == k)]
                        if len(ids):
                            x, y, _ = segments(links.loc[ids])
                            fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", line=dict(color=col, width=3, dash=dash),
                                                       name=f"{k}: {cl} ({len(ids)})", text=np.repeat(ids.values, 3),
                                                       hoverinfo="text"))
                FIG["intersections_map"] = _map_layout(fig, "Pipe crowns / channel banks vs street surface")

        # length above the surface by threshold (pieces <= piece, evaluated at midpoints, weighted by SWMM length)
        X, Y, ZT, W, K = [], [], [], [], []
        for name, r in pcs.iterrows():
            xs, ys, zt, n = link_points(name, r, cfg.piece, True)
            X.append(xs); Y.append(ys); ZT.append(zt)
            W.append(np.full(n, (r.length if r.length > 0 else r.plan_len) / n)); K.append(np.full(n, r.role))
        lrows = []
        if X:
            X, Y, ZT, W, K = map(np.concatenate, (X, Y, ZT, W, K))
            ex = ZT - surface_z(X, Y)
            curves = {}
            for k in ["pipe", "channel"]:
                m = K == k
                if not m.any():
                    continue
                tot = W[m].sum()
                cov = m & ~np.isnan(ex)
                ass = W[cov].sum()
                for t in cfg.thresholds:
                    la = W[cov & (ex > t)].sum()
                    lrows.append(dict(kind=k, threshold=t, length_above=la, pct_assessed=100 * la / ass if ass else np.nan,
                                      pct_total=100 * la / tot, assessed=ass, total=tot))
                if cov.any():
                    o = np.argsort(ex[cov])
                    curves[k] = (ex[cov][o], 100 * (1 - np.cumsum(W[cov][o]) / ass))
            LA = pd.DataFrame(lrows)
            TAB["length_above_surface"] = LA.round(4)
            R["length_above"] = LA.round(3).to_dict("records")
            if curves:
                lo = min(float(np.percentile(v[0], 1)) for v in curves.values())
                hi = max(float(np.percentile(v[0], 99.9)) for v in curves.values())
                tc = np.linspace(min(lo, -0.5), max(hi, cfg.thresholds[-1]), 200)
                fig = go.Figure()
                for k, col, nm in [("pipe", "#d62728", "pipes (crown)"), ("channel", "#1f3fff", "channels (bank)")]:
                    if k in curves:
                        e, cum = curves[k]
                        fig.add_trace(go.Scatter(x=tc, y=np.interp(tc, e, cum, left=100, right=0), mode="lines", name=nm,
                                                 line=dict(color=col, width=3),
                                                 hovertemplate=f"top − surface > %{{x:.2f}} {U}: %{{y:.1f}} % of length<extra>{nm}</extra>"))
                fig.add_vline(x=0, line_dash="dot", line_color="grey")
                fig.update_layout(title="Share of the assessed length whose crown/bank is more than X above the surface",
                                  xaxis_title=f"threshold X = top − surface ({U})", yaxis_title="% of assessed length",
                                  yaxis=dict(range=[0, 100]), height=500, template="plotly_white")
                FIG["length_curve"] = fig

    # ------------------------------------------------------------------ 8. additional checks
    A = {}
    for k in ["pipe", "channel", "street"]:
        d = links[links.role == k]
        if not len(d):
            continue
        d1, d2 = d.off1 - d.e_from, d.off2 - d.e_to
        sl = 100 * (d.off1 - d.off2) / d.length
        ratio = d.length / d.plan_len
        A[k] = dict(n=len(d), below_node=int((d1 < -TOL).sum() + (d2 < -TOL).sum()),
                    drop=int((d1 > TOL).sum() + (d2 > TOL).sum()), adverse=int((sl < -1e-4).sum()),
                    flat=int((sl.abs() <= 1e-4).sum()), steep=int((sl.abs() > cfg.steep_pct).sum()),
                    slope_median=round(float(sl.median()), 3), slope_max=round(float(sl.abs().max()), 2),
                    len_ratio_median=round(float(ratio.median()), 3), len_ratio_lt09=int((ratio < 0.9).sum()),
                    len_ratio_gt15=int((ratio > 1.5).sum()), zero_len=int((d.length <= 0).sum()),
                    no_xsection=int((d["shape"] == "").sum()))
    neg_off = links[links.role.isin(["pipe", "channel", "street"]) &
                    (((links.off1 - links.e_from) < -TOL) | ((links.off2 - links.e_to) < -TOL))]
    TAB["offsets_below_node"] = neg_off[["role", "from", "to", "e_from", "off1", "e_to", "off2"]].reset_index().round(3)
    slp = links[links.role.isin(["pipe", "channel", "street"])].assign(slope_pct=lambda d: 100 * (d.off1 - d.off2) / d.length)
    TAB["adverse_or_steep_links"] = slp[(slp.slope_pct < -1e-4) | (slp.slope_pct.abs() > cfg.steep_pct)][
        ["role", "from", "to", "off1", "off2", "length", "slope_pct"]].reset_index().round(3)
    A["self_loops"] = int((links["from"] == links["to"]).sum())
    pair = pd.Series([tuple(sorted(p)) for p in zip(links["from"], links["to"])], index=links.index)
    dup = links.assign(pair=pair).groupby("pair").filter(lambda g: len(g) > 1)
    A["parallel_groups"] = int(dup.pair.nunique()) if len(dup) else 0
    A["parallel_by_roles"] = dup.groupby("pair").role.agg(lambda s: "+".join(sorted(s))).value_counts().to_dict() if len(dup) else {}
    if len(dup):
        TAB["parallel_links"] = dup[["role", "ltype", "from", "to", "length"]].assign(pair=dup.pair.astype(str)).reset_index()
    # storage invert vs street neighbours
    if len(st_l):
        nb = pd.concat([st_l[["from", "to"]].set_axis(["a", "b"], axis=1), st_l[["to", "from"]].set_axis(["a", "b"], axis=1)])
        nb = nb[nb.a.map(nodes["type"]) == "STORAGE"]
        nb["eb"] = nb.b.map(nodes.elev)
        st_main = storage[storage.comp == 0]
        out = pd.DataFrame({"elev": st_main.elev, "nb_median": nb.groupby("a").eb.median(), "n_nb": nb.groupby("a").size()}).dropna()
        out["diff"] = out.elev - out.nb_median
        A["outliers"] = {f"{t:g}": int((out["diff"].abs() > t).sum()) for t in cfg.outlier}
        o = out[out["diff"].abs() > cfg.outlier[0]].sort_values("diff", key=abs, ascending=False)
        TAB["storage_elev_outliers"] = o.rename_axis("node").reset_index().round(3)
        A["outlier_top"] = o.head(3).round(3).rename_axis("node").reset_index().to_dict("records")
        if R["has_coords"] and len(o):
            o = o.join(storage[["x", "y"]])
            fig = go.Figure()
            x, y, _ = segments(st_l)
            fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", line=dict(color="#e5e5e5", width=1), name="streets"))
            lim = max(float(o["diff"].abs().max()), 1)
            fig.add_trace(go.Scattergl(x=o.x, y=o.y, mode="markers", name=f"|elev − median(neighbours)| > {cfg.outlier[0]:g} {U}",
                                       marker=dict(color=o["diff"], colorscale="RdBu_r", cmin=-lim, cmax=lim, size=8,
                                                   colorbar=dict(title=f"diff ({U})")),
                                       text=[f"{n}<br>elev {e:.2f}<br>neighbour median {m:.2f}" for n, e, m in zip(o.index, o.elev, o.nb_median)],
                                       hoverinfo="text"))
            FIG["outliers_map"] = _map_layout(fig, "Storage nodes departing from the median of their street neighbours")
    # junctions
    junc = nodes[(nodes["type"] == "JUNCTION") & (nodes.comp == 0)].copy()
    if len(junc):
        cpe = cp.assign(es=cp.s.map(nodes.elev))
        jag = cpe.groupby("j").agg(n_coupled=("s", "size"), st_min=("es", "min"), st_mean=("es", "mean"))
        junc = junc.join(jag)
        junc["net"] = junc.index.map(jkind).fillna("none")
        junc["max_top"] = E.groupby("junction").top.max()
        A["junction"] = dict(n=len(junc), net=junc.net.value_counts().to_dict(),
                             uncoupled=int(junc.n_coupled.isna().sum()),
                             uncoupled_by_net=junc[junc.n_coupled.isna()].net.value_counts().to_dict(),
                             invert_above_coupled_storage=int((junc.elev - junc.st_min > TOL).sum()),
                             maxdepth=_stats(junc.maxdepth, 2),
                             rim_minus_street=_stats(junc.elev + junc.maxdepth - junc.st_mean, 2),
                             depth_to_street=_stats(junc.st_mean - junc.elev, 2))
        for r in COUPLING_ROLES:
            d = cpl[cpl.role == r]
            if len(d):
                diff = d.crest - d.junction.map(junc.max_top)
                A[f"{r}_crest_below_top"] = dict(n=int(diff.notna().sum()), below=int((diff < -TOL).sum()),
                                                 below_05=int((diff < -cfg.thresholds[4]).sum()))
        ov = cpl[cpl.role == "overflow"]
        if len(ov):
            bank = ov.junction.map(E[E.kind == "channel"].groupby("junction").top.max())
            A["overflow_crest_minus_bank"] = _stats(ov.crest - bank)
    deg = pd.Series(dict(G.degree()))
    A["degree1"] = nodes["type"][deg[deg == 1].index].value_counts().to_dict()
    A["degree0"] = int((deg == 0).sum())
    ol = links[links.role == "outfall_link"]
    A["outfalls"] = ol[["ltype", "from", "to", "e_from", "off1", "e_to", "off2", "length"]].assign(
        from_type=ol["from"].map(nodes["type"]), from_net=ol["from"].map(jkind).fillna("")).round(3).reset_index().to_dict("records")
    A["n_other_links"] = links[links.role == "other"].ltype.value_counts().to_dict()
    if (links.role == "other").any():
        TAB["other_links"] = links[links.role == "other"][["ltype", "from", "to", "ft", "shape"]].reset_index()
    R["additional"] = A
    return Results(R=R, tables=TAB, figs=FIG)

"""Writes the geometry consistency report (Markdown + standalone interactive HTML) from checks.Results."""
import datetime as dt
import html
import math
import re

import markdown
import pandas as pd
import plotly.offline

FIGTAG = "[[FIG:{}]]"


# --------------------------------------------------------------------------------------------- formatting
def n(x, d=0):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return f"{x:,.{d}f}"


def fd(d):
    """Format a {key: count} dict as 'key 12, other 3'."""
    if not d:
        return "none"
    return ", ".join(f"{k} {n(v) if isinstance(v, (int, float)) else v}" for k, v in d.items())


def pl(k, word, plural=None):
    """'1 link' / '3 links'."""
    return f"{n(k)} {word if k == 1 else (plural or word + 's')}"


def pct(a, b, d=1):
    return "–" if not b else f"{100 * a / b:.{d}f} %"


def md_table(df, cols=None, rename=None, fmt=None, max_rows=30):
    if df is None or len(df) == 0:
        return "_(none)_"
    d = df[cols] if cols else df
    more = len(d) - max_rows
    d = d.head(max_rows)
    fmt = fmt or {}
    head = [str((rename or {}).get(c, c)) for c in d.columns]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for _, r in d.iterrows():
        cells = []
        for c in d.columns:
            v = r[c]
            if c in fmt:
                v = fmt[c](v)
            elif isinstance(v, float):
                v = "–" if math.isnan(v) else (f"{v:,.3f}" if abs(v) < 1000 else f"{v:,.0f}")
            cells.append(str(v).replace("|", "/"))
        out.append("| " + " | ".join(cells) + " |")
    if more > 0:
        out.append(f"\n_… {more} more rows in the CSV table._")
    return "\n".join(out)


# --------------------------------------------------------------------------------------------- report
def build_markdown(model, res, out_tables_dir="tables"):
    R, T = res.R, res.tables
    U = R["unit"]
    AU = "ha" if U == "m" else "ac"
    cfg = R["cfg"]
    th = cfg["thresholds"]
    tol = cfg["tol"]
    L = []
    w = L.append
    has = lambda f: f in res.figs  # noqa: E731

    w(f"# Geometric consistency of the SWMM drainage graph — {html.escape(model.path.name)}")
    w("")
    w(f"*Model:* `{model.path.name}` ({html.escape(model.title)}). "
      f"`FLOW_UNITS = {R['flow_units']}` (lengths in **{U}**), `LINK_OFFSETS = {R['offsets_mode']}`, "
      f"`FLOW_ROUTING = {R['routing']}`.  ")
    w(f"*Generated:* {dt.date.today().isoformat()} by `swmm_geomcheck`. Tables: `{out_tables_dir}/*.csv`; all numbers: `results.json`.")
    w("")
    w(f"All link offsets and crest heights were converted to **absolute elevations** "
      f"({'given as elevations in the file' if R['offsets_mode'] == 'ELEVATION' else 'node invert + offset, since the file gives offsets as depths'}), "
      f"so every comparison below is between elevations. Equality is tested with a tolerance of {tol:g} {U}.")
    if R["warnings"]:
        w("")
        w("**Warnings while reading the model:**")
        w("")
        for x in R["warnings"]:
            w(f"* {x}")
    w("")
    w("[TOC]")
    w("")

    # ---------------------------------------------------------------- roles
    w("## Link roles")
    w("")
    w("Links are classified from the network topology and the cross-section shape, not from their names:")
    w("")
    w("| role | definition | count |")
    w("|---|---|---|")
    defs = dict(street="CONDUIT with at least one STORAGE end (surface flow)",
                pipe="CONDUIT between junctions, closed cross-section",
                channel="CONDUIT between junctions, open cross-section (IRREGULAR, RECT_OPEN, TRAPEZOIDAL, …)",
                inlet="ORIFICE between a STORAGE node and a JUNCTION (street → sub-surface)",
                overflow="WEIR between a STORAGE node and a JUNCTION (street ↔ sub-surface)",
                outfall_link="any link touching an OUTFALL",
                other="pumps, outlets, and orifices/weirs that do not join a storage node to a junction")
    for r, c in R["n_links_role"].items():
        w(f"| {r} | {defs[r]} | {n(c)} |")
    w("")
    w("STORAGE nodes are taken as the **street surface** (dual-drainage representation) and JUNCTIONS as the "
      "sub-surface network. *Couplings* are the inlets and overflows. Nodes: "
      + ", ".join(f"{n(v)} {k.lower()}" for k, v in R["n_nodes"].items()) + ".")
    if cfg.get("role_regex"):
        w("")
        w("Roles forced by name: " + ", ".join(f"`{k}` ← `{v}`" for k, v in cfg["role_regex"].items()) + ".")
    if R["n_nodes"].get("STORAGE", 0) == 0:
        w("")
        w("> **This model has no STORAGE nodes, so it has no street-surface layer.** The checks that compare the "
          "sub-surface network with the street surface (sections 2–7) do not apply.")
    w("")
    w(md_table(T["link_roles"], rename={"ft": "from → to type", "ltype": "SWMM type"}))
    w("")

    # ---------------------------------------------------------------- 1
    mi = R["minor"]
    comps = T["components"]
    w("## 1. Disconnected subgraphs")
    w("")
    w(f"The undirected graph (all nodes; all links as edges) has **{pl(R['n_components'], 'connected component')}**. "
      f"The main one holds {n(R['main']['n_nodes'])} nodes, {n(R['main']['n_links'])} links and "
      f"{n(R['main']['n_outfall'])} outfall(s).")
    w("")
    if mi["n"]:
        w(f"The other {pl(mi['n'], 'component')} hold {pl(mi['n_nodes'], 'node')} and {pl(mi['n_links'], 'link')}; "
          f"{n(mi['n_with_outfall'])} of them contain an outfall. {pl(mi['n_subcatch'], 'subcatchment')} "
          f"({n(mi['subcatch_area'], 2)} {AU}) drain into them, compared with {n(mi['main_subcatch_area'], 2)} {AU} "
          "draining to the main component.")
        w("")
        cols = [c for c in ["comp", "n_nodes", "n_links", "n_storage", "n_junction", "n_outfall", "elev_min", "elev_max",
                            "nearest_main_node", "dist_to_main", "elev_nearest_main", "gap", "n_subcatch", "subcatch_area",
                            "nodes"] if c in comps]
        w(md_table(comps.iloc[1:], cols, rename={"gap": f"gap ({U})", "dist_to_main": f"distance ({U})"}))
        w("")
        w(f"`gap` is the elevation of the component's node closest to the main graph minus the elevation of that main-graph node.")
        w("")
        if mi["n_gap_flag"]:
            w(f"**{pl(mi['n_gap_flag'], 'component differs', 'components differ')} by more than {cfg['gap_flag']:g} {U} in elevation from the "
              f"main graph next to them** (median gap {n(mi['gap_median'], 1)} {U}). Elements raised far above their "
              "surroundings are usually elements that were *switched off* rather than deleted. They are not physically "
              "consistent with the surrounding surface.")
        else:
            w("The isolated components lie at elevations similar to their surroundings. They may be missing "
              "connections (check them on the map).")
        w("")
        w("**Recommendation.** Remove the isolated components, or connect them if they represent real elements. "
          "Reassign their subcatchments to the nearest main-graph node. Isolated pieces have no message path to the "
          "rest of the graph in a GNN, and far-off elevations distort node-feature normalisation. They are excluded "
          "from the surface used in sections 3–7.")
    else:
        w("The graph is fully connected.")
    w("")
    if has("components_map"):
        w(FIGTAG.format("components_map"))
        w("")
    sl, ss = R["surface_layer"], R["subsurface_layer"]
    w("**Layer connectivity.**")
    w("")
    w(f"* *Surface layer* (storage nodes + streets): {n(sl['n'])} components, largest sizes {sl['sizes']}, "
      f"{n(sl['singletons'])} single nodes. Street patches are reconnected only through the sub-surface network (couplings).")
    w(f"* *Sub-surface layer* (junctions, outfalls + pipes, channels, outfall links between them): {n(ss['n'])} components, "
      f"largest sizes {ss['sizes']}. **{n(ss['n_no_outfall'])} networks** ({n(ss['nodes_no_outfall'])} nodes, "
      f"{n(ss['pipes_no_outfall'])} pipes, {n(ss['channels_no_outfall'])} channels) reach no outfall. They exchange "
      "water with the street only through their couplings.")
    w("")

    # ---------------------------------------------------------------- 2
    s2 = R["nsj"]
    w("## 2. Storage ↔ junction connections that are not orifices or weirs")
    w("")
    if not s2["n"]:
        w("None: every link between a storage node and a junction is an orifice or a weir.")
    else:
        w(f"**{n(s2['n'])} links** join a storage node to a junction without being an orifice or weir "
          f"(by role: {fd(s2['by_role'])}; {fd(s2['by_direction'])}). They touch {n(s2['n_junctions'])} junctions "
          f"(network at the junction: {fd(s2['junction_net'])}); {n(s2['junctions_multi'])} junctions receive more than one.")
        w("")
        j = s2["j_end_above_junction"]; sm = s2["storage_minus_j_end"]; sp = s2["slope"]
        w("| statistic | value |")
        w("|---|---|")
        w(f"| invert at the storage end = storage invert | {n(s2['s_end_match'])} of {n(s2['n'])} |")
        if j:
            w(f"| invert at the junction end − junction invert | {j['min']:.2f} to {j['max']:.2f} {U} (median {j['50%']:.2f}) |")
            w(f"| storage invert − invert at the junction end | {sm['min']:.2f} to {sm['max']:.2f} {U} (median {sm['50%']:.2f}) |")
            w(f"| slope | {sp['min']:.2f} to {sp['max']:.2f} % (median {sp['50%']:.2f} %) |")
        w("")
        w(md_table(T["storage_junction_noncoupling"], ["name", "role", "direction", "junction", "junction_net", "storage",
                                                       "length", "elev_storage", "elev_junction", "inv_junction_end",
                                                       "inv_j_end_minus_junction", "storage_minus_inv_j_end", "slope_pct"]))
        w("")
        if j and abs(sm["50%"]) < 0.25 * (1 if U == "m" else 3.28) and j["min"] > 0:
            w("**Plausibility.** The ends on the storage side match the street invert. On the junction side the invert "
              "sits close to street level, well above the junction bed. The link therefore behaves like a lateral spillway "
              "between the street and the sub-surface element. This is geometrically consistent. When the junction is a "
              "channel node and several streets meet there, the pattern is typically a street crossing a channel routed "
              "through the channel node. For a surrogate model these links deserve their own edge type.")
        else:
            w("**Plausibility.** Check the rows above. A storage-side invert different from the storage invert, or a "
              "junction-side invert far below street level, means the link does not follow the street surface.")
    w("")

    # ---------------------------------------------------------------- 3
    se = R["street_ends"]
    w("## 3. Storage-node inverts vs street-end inverts, and the street surface")
    w("")
    if se["n"]:
        if se["n_mismatch"] == 0:
            w(f"* **No mismatch**: at all {n(se['n'])} street ends that land on a storage node, the street invert equals the "
              f"storage invert (maximum |difference| {se['max_abs']:.3f} {U}).")
        else:
            m = se["mismatch"]
            w(f"* **{n(se['n_mismatch'])} of {n(se['n'])} street ends** ({n(se['n_storage_nodes_mismatch'])} storage nodes) have "
              f"an invert different from the storage invert (difference from {m['min']:.3f} to {m['max']:.3f} {U}; "
              f"positive = street end above the node, i.e. a drop). See `tables/street_end_mismatch.csv`.")
            w("")
            w(md_table(T["street_end_mismatch"].sort_values("diff", key=abs, ascending=False), max_rows=15))
        w(f"* {n(se['storage_without_street'])} storage nodes have no street. They are connected only through couplings or "
          "outfall links (`tables/storage_without_street.csv`).")
    else:
        w("The model has no street links.")
    w("")
    if R["surface_ok"]:
        t = R["tin"]
        w(f"**Street surface.** The *street surface* is the linear interpolation (Delaunay TIN) of the inverts of the "
          f"{n(R['n_surface_nodes'])} storage nodes of the main component. Triangles with an edge longer than "
          f"{cfg['tin_max_edge']:g} {U} are dropped, so the surface does not bridge areas without streets. "
          f"Kept: {n(t['n_keep'])} of {n(t['n_tri'])} triangles, {t['area_keep_km2']} km²; dropped: {t['area_drop_km2']} km²"
          f"{' (areas in km² assume metre coordinates)' if U == 'ft' else ''}. Surface elevation: "
          f"{t['elev']['min']:.2f} to {t['elev']['max']:.2f} {U} (median {t['elev']['50%']:.2f}). "
          "The method is detailed in §7.1.")
        w("")
        for f in ["surface_3d", "surface_map"]:
            if has(f):
                w(FIGTAG.format(f))
                w("")
    else:
        w("_Not enough storage nodes with coordinates to build a street surface; sections 6–7 are skipped._")
        w("")

    # ---------------------------------------------------------------- 4
    rv = R["reversed"]
    w("## 4. Couplings oriented junction → storage")
    w("")
    for r in ["inlet", "overflow"]:
        c = R["couplings"][r]
        if not c["n"]:
            continue
        cm = c["crest_minus_storage"]
        w(f"* **{r}s** ({'orifices' if r == 'inlet' else 'weirs'}): {n(c['n'])} (types: {fd(c['types'])}; gated: {n(c['gated'])}). "
          f"In the storage → junction orientation, the crest − storage invert is "
          + (f"{cm['min']:.3f} to {cm['max']:.3f} {U} (most frequent {c['crest_minus_storage_mode']:.3f}); "
             f"{n(c['crest_eq_storage'])} have their crest exactly at the street invert." if cm else "–.")
          + (f" Distance between the coupled nodes: median {c['dist']['50%']:.1f} {U}, max {c['dist']['max']:.1f} {U}." if c["dist"] else ""))
    w("")
    if rv["n"]:
        cm = rv["crest_minus_storage"]
        w(f"**{pl(rv['n'], 'coupling is', 'couplings are')} reversed** (FromNode = junction, ToNode = storage; {fd(rv['by_role'])}). "
          f"Their crest − storage invert ranges from {cm['min']:.3f} to {cm['max']:.3f} {U}; "
          f"{n(rv['n_crest_above_storage'])} have their crest above the street they serve. "
          f"{n(rv['n_crest_eq_sibling'])} have a crest identical to another coupling of the same junction (value probably "
          f"copied), and {n(rv['n_storage_below_junction'])} connect a street lower than the junction invert.")
        w("")
        w(md_table(T["reversed_couplings"], ["name", "role", "from", "to", "crest", "elev_junction", "elev_storage",
                                             "crest_minus_storage", "dist", "gated", "crest_equals_sibling",
                                             "storage_below_junction_invert"]))
        w("")
        if rv["n_crest_above_storage"]:
            w("A crest above the street invert means the coupling only activates once the street water depth exceeds that "
              "difference. Such reversed couplings **do not represent the street geometry correctly**. Check them in a GIS: "
              "reorient them storage → junction with the crest at the street invert, or attach them to the right storage node.")
            w("")
    else:
        w("**No reversed couplings**: every inlet and overflow goes from the storage node to the junction.")
        w("")
    w("**Does the FromNode/ToNode direction matter in SWMM?** For ungated orifices and weirs it is essentially a sign "
      "convention:")
    w("")
    w("* the flow is computed from the head difference between the two ends, and its sign comes from whichever end is "
      "higher; reverse flow uses the same equations, so reversing the ends only flips the sign of the reported flow;")
    w("* the crest is stored internally as an offset above the **FromNode invert**. With `LINK_OFFSETS = ELEVATION` the "
      "absolute crest is kept whatever the direction, *unless it is below the FromNode invert*. In that case SWMM resets "
      "the offset to 0 (with a warning), which raises the crest. With `DEPTH` offsets, the same number means a different "
      "elevation when the ends are swapped;")
    w("* gated (flap-gate) links only allow flow From → To, so for them the direction is physical.")
    w("")
    w("For a surrogate, store crests as absolute elevations so that the edge direction carries no geometric meaning.")
    w("")

    # ---------------------------------------------------------------- 5
    w("## 5. Pipe crowns and channel banks vs the street nodes they are coupled to")
    w("")
    w("**Method.**")
    w("")
    w("* The check is done **per connection**. A connection is one pipe or channel end, at junction J, coupled to one "
      "storage node S through one inlet (orifice) or overflow (weir) between S and J.")
    w("* Ends whose junction has no inlet or overflow are **not assessed**. An end with several couplings is "
      "assessed **separately for each storage node**.")
    w("* *Top* elevation of an end:")
    w("  * pipe crown = end invert + full height of the section (`Geom1`; `Geom1 − Geom2` for FILLED_CIRCULAR);")
    w("  * channel bank = end invert + bank depth. This is `Geom1` for regular open shapes; for IRREGULAR sections it "
      "comes from the transect (below).")
    w("* **Cover = storage invert − top.** A negative value means the crown or bank is above the street node it exchanges "
      "water with.")
    w("")
    if "transect_banks" in T:
        tb = T["transect_banks"]
        tbc = tb[tb.get("n_channel", 0) > 0] if "n_channel" in tb else tb
        w("**Bank identification from IRREGULAR transects** (`tables/transect_banks.csv`).")
        w("")
        w("1. The outer near-vertical segments (|Δx| ≤ 0.1) are removed. These are the 'glass walls' that extend the section.")
        w("2. The main channel lies between the X1 left/right bank stations, or spans the whole remaining profile when "
          "both are 0.")
        w("3. The bank on each side is the highest point between the bank station and the thalweg. The bank depth is the "
          "lower of the two.")
        w("")
        if len(tbc):
            w(md_table(tbc.sort_values("n_channel", ascending=False), ["transect", "lbank_sta", "rbank_sta", "x1_banks_given",
                                                                     "bank_left", "bank_right", "bank_depth", "full_depth",
                                                                     "n_channel"], max_rows=20))
            w("")
    if R["pc_no_top"]:
        w(f"_{n(R['pc_no_top'])} pipes/channels have no usable section height (e.g. STREET or missing transect) and are skipped._")
        w("")
    cv = R["cover"]
    if R["n_conn_total"]:
        rows = [("links with ≥ 1 coupled end / total", lambda c: f"{n(c.get('n_links'))} / {n(c['total_links'])}"),
                ("coupled ends / total ends", lambda c: f"{n(c.get('n_ends'))} / {n(c['total_ends'])}"),
                ("**connections assessed**", lambda c: f"**{n(c['n_conn'])}** ({fd(c.get('by_coupling'))})"),
                ("connections per end", lambda c: ", ".join(f"{k}: {n(v)}" for k, v in c.get("conn_per_end", {}).items()) + " (connections: ends)"),
                (f"ends whose coupled storage inverts differ by > {0.5 if U == 'm' else 1.64:g} {U}", lambda c: f"{n(c.get('spread_gt'))} (max spread {n(c.get('spread_max'), 2)} {U})"),
                ("median cover", lambda c: f"**{c['stats']['50%']:+.2f} {U}**" if c.get("stats") else "–"),
                ("5 % – 25 % – 75 % – 95 %", lambda c: " / ".join(f"{c['stats'][k]:+.2f}" for k in ["5%", "25%", "75%", "95%"]) if c.get("stats") else "–"),
                (f"\\|cover\\| ≤ {th[1]:g} {U}", lambda c: n(c.get("n_zero"))),
                ("**top above the street node (cover < 0)**", lambda c: f"**{n(c.get('n_neg'))} ({c.get('pct_neg', '–')} %)**"),
                (f"… by more than {th[4]:g} {U}", lambda c: n(c.get("n_neg_05"))),
                (f"… by more than {th[6]:g} {U}", lambda c: n(c.get("n_neg_1"))),
                ("ends: ≥ 1 negative / all negative / mixed signs", lambda c: f"{n(c.get('ends_any_neg'))} / {n(c.get('ends_all_neg'))} / {n(c.get('ends_mixed'))}"),
                ("links with ≥ 1 negative connection", lambda c: n(c.get("links_any_neg")))]
        w("**Results** (`tables/connections.csv`, one row per connection):")
        w("")
        w("| | pipes | channels |")
        w("|---|---|---|")
        for lab, f in rows:
            w(f"| {lab} | {f(cv['pipe']) if cv['pipe'].get('n_conn') else '–'} | {f(cv['channel']) if cv['channel'].get('n_conn') else '–'} |")
        w("")
        p = cv["pipe"]
        if p.get("n_conn"):
            w("**Pipes.**")
            w("")
            if p["n_neg"]:
                w(f"* {n(p['n_neg'])} connections have the crown above the coupled street node. In "
                  f"**{n(p['neg_shallow_manhole'])} of them the pipe is taller than the depth between that street node and the "
                  "junction invert**, i.e. the junction (manhole) is too shallow for the pipe.")
                if p.get("by_size"):
                    w("* Most affected sections: " + "; ".join(f"{b['size']} ({b['n_neg']} of {b['n']})" for b in p["by_size"][:6]) + ".")
                if p["ends_mixed"]:
                    w(f"* At {n(p['ends_mixed'])} pipe ends the sign depends on which coupled street is taken: the crown is "
                      "below one street node and above another, because the storage nodes around one junction have different "
                      "inverts. Assessing each connection separately shows these ends.")
                w("* Worst connections:")
                w("")
                w(md_table(pd.DataFrame(p["worst"])))
                w("")
                w("When the crown is above the street, the inlet crest (street level) lies below the pipe crown. The pipe "
                  "pressurises before the street floods, and backflow to the street starts at a lower head than the real "
                  "geometry would allow.")
            else:
                w("* Every pipe crown is below the street nodes it is coupled to.")
            w("")
        c = cv["channel"]
        if c.get("n_conn"):
            w("**Channels.**")
            w("")
            for cr, s in c["by_coupling_stats"].items():
                w(f"* Through {cr}s ({n(s['n'])} connections): median cover {s['median']:+.2f} {U}; {n(s['n_neg'])} with the "
                  "bank above the street.")
            w("* A bank close to street level (cover ≈ 0) is the expected design when streets spill into the channel over "
              "its banks. A bank well above the coupled street means either an embankment/levee, or a channel invert or "
              "bank depth set too high. In that case the coupling spills below the modelled bank.")
            w("* Worst connections:")
            w("")
            w(md_table(pd.DataFrame(c["worst"])))
            w("")
        for f in ["cover_hist", "cover_map"]:
            if has(f):
                w(FIGTAG.format(f))
                w("")
    else:
        w("_No pipe or channel end is coupled to a storage node: this check does not apply._")
        w("")

    # ---------------------------------------------------------------- 6
    if R["surface_ok"]:
        w("## 6. 3D view: surface, pipe crowns and channel banks")
        w("")
        w("Surface: the TIN of §3. Red: pipe crowns. Blue: channel banks (the top elevation at each end, linear in "
          "between, drawn as a straight segment between the end nodes, ignoring vertices). Hover a line for its name. Click legend items "
          "to toggle layers. The vertical scale is exaggerated.")
        w("")
        if has("crowns_banks_3d"):
            w(FIGTAG.format("crowns_banks_3d"))
            w("")

        # ------------------------------------------------------------ 7
        t = R["tin"]
        w("## 7. Intersections between the surface and the crown/bank lines")
        w("")
        w("### 7.1 Method")
        w("")
        w("**Step 1 – Reference surface (street surface).**")
        w("")
        w(f"* *Input points*: (x, y, invert) of the {n(R['n_surface_nodes'])} storage nodes of the main component, in the "
          "projected coordinates of the `.inp`. The storage nodes of disconnected components are excluded.")
        w("* *What it represents*: the **street invert** (gutter line, i.e. the lowest point of the street section), not a "
          "terrain DEM.")
        w(f"* *Triangulation*: Delaunay triangulation in the horizontal plane (`scipy.spatial.Delaunay`): {n(t['n_tri'])} triangles.")
        w(f"* *Masking*: triangles whose longest edge exceeds {cfg['tin_max_edge']:g} {U} are removed "
          f"({n(t['n_tri'] - t['n_keep'])} triangles). They bridge areas with no street nodes, where an interpolated "
          f"street is meaningless. The median longest edge of a triangle is {t['median_edge']:g} {U}.")
        w("* *Interpolation*: inside each kept triangle, the elevation is the linear (barycentric) interpolation of its "
          "three vertices (`scipy.interpolate.LinearNDInterpolator`), i.e. the plane through the three nodes. There is no "
          "smoothing or extrapolation. Points outside the kept triangles are *not assessable*.")
        w("")
        w("**Step 2 – Crown/bank lines.**")
        w("")
        w("* Each pipe or channel follows its plan geometry: from-node, `[VERTICES]` if any "
          f"({n(R['n_links_with_vertices'])} links have vertices), to-node.")
        w("* The top elevation (§5) is set at each end. Along the link it varies **linearly with the distance travelled**, "
          "i.e. constant slope and section.")
        w("")
        w(f"**Step 3 – Per-link classification** (`tables/intersections.csv`).")
        w("")
        w(f"* Points are spaced at most {cfg['class_step']:g} {U}, both ends included. At each point, e = top − surface; "
          "points without a surface value are dropped.")
        w(f"* A link is *below surface* if every assessed point has e ≤ {tol:g} {U}, *entirely above* if every point is "
          "above, *crosses surface* if some are above and some below (`n_crossings` counts the sign changes), and "
          "*outside surface* if no point is assessable.")
        w("* *Severity* is the maximum exceedance along the link.")
        w("")
        w(f"**Step 4 – Length above the surface by threshold** (`tables/length_above_surface.csv`).")
        w("")
        w(f"* Each link is cut into equal pieces of at most {cfg['piece']:g} {U}. Each piece is evaluated at its midpoint "
          "and weighted by its share of the SWMM link length.")
        w("* **Only the part of a link that is above counts**: a link crossing the surface contributes partially.")
        w("* For each threshold X: length above(X) = Σ weights of the pieces with e > X. It is given as a % of the "
          "assessed length (pieces with a surface value) and of the total length. The curve shows the same quantity "
          "for every X.")
        w("")
        w("**Limitations.**")
        w("")
        w(f"* The surface is planar between street nodes (typical spacing {t['median_edge']:g} {U}). Exceedances of a few "
          "decimetres are within its uncertainty.")
        w("* Across a channel, the triangles join the streets on both banks, so a bank is compared with a plane between "
          "those streets.")
        w("* A DEM would give a more direct check of the terrain.")
        w("")
        w("### 7.2 Results")
        w("")
        if "intersections" in R:
            I = R["intersections"]
            classes = ["below surface", "crosses surface", "entirely above surface", "outside surface"]
            w("| class | pipes | channels |")
            w("|---|---|---|")
            for cl in classes:
                w(f"| {cl} | {n(I['pipe']['classes'].get(cl, 0))} | {n(I['channel']['classes'].get(cl, 0))} |")
            w("")
            sev = list(I["pipe"]["severity"].keys())
            w(f"Maximum exceedance per link ({U}):")
            w("")
            w("| | " + " | ".join(sev) + " |")
            w("|---|" + "---|" * len(sev))
            for k in ["pipe", "channel"]:
                w(f"| {k}s | " + " | ".join(n(I[k]["severity"].get(s, 0)) for s in sev) + " |")
            w("")
            if has("intersections_map"):
                w(FIGTAG.format("intersections_map"))
                w("")
        if "length_above" in R and R["length_above"]:
            LA = pd.DataFrame(R["length_above"])
            w("**Share of the network length above the surface, by threshold.**")
            w("")
            hdr = "| threshold (top − surface) |"
            sep = "|---|"
            kinds = [k for k in ["pipe", "channel"] if k in set(LA.kind)]
            for k in kinds:
                hdr += f" {k}s: length above ({U}) | {k}s: % of assessed | {k}s: % of total |"
                sep += "---|---|---|"
            w(hdr)
            w(sep)
            for t_ in sorted(set(LA.threshold)):
                row = f"| > {t_:g} {U} |"
                for k in kinds:
                    r = LA[(LA.kind == k) & (LA.threshold == t_)].iloc[0]
                    row += (f" {n(r.length_above)} | {r.pct_assessed:.2f} % | {r.pct_total:.2f} % |" if r.assessed > 0
                            else " – | not assessable | – |")
                w(row)
            w("")
            w("Assessed / total length: " + "; ".join(
                f"{k}s {n(LA[LA.kind == k].assessed.iloc[0])} / {n(LA[LA.kind == k].total.iloc[0])} {U}" for k in kinds) + ".")
            w("")
            if has("length_curve"):
                w(FIGTAG.format("length_curve"))
                w("")

    # ---------------------------------------------------------------- 8
    A = R["additional"]
    w("## 8. Additional verifications")
    w("")
    w("| check | result |")
    w("|---|---|")
    for k in ["pipe", "channel", "street"]:
        if k in A:
            a = A[k]
            w(f"| {k}s: offsets below the node invert / drops above it | {n(a['below_node'])} / {n(a['drop'])} |")
            w(f"| {k}s: slope (from end inverts) | median {a['slope_median']:.3f} %, {n(a['flat'])} flat, "
              f"**{n(a['adverse'])} adverse**, {n(a['steep'])} steeper than {cfg['steep_pct']:g} %, max {a['slope_max']:.2f} % |")
            w(f"| {k}s: SWMM length / plan length | median {n(a['len_ratio_median'], 3)}; {n(a['len_ratio_lt09'])} < 0.9, "
              f"{n(a['len_ratio_gt15'])} > 1.5; {n(a['zero_len'])} zero length; {n(a['no_xsection'])} without cross-section |")
    w(f"| self-loops / groups of parallel links | {n(A['self_loops'])} / {n(A['parallel_groups'])} {('(' + fd(A['parallel_by_roles']) + ')') if A['parallel_by_roles'] else ''} |")
    if "outliers" in A:
        w("| storage invert vs median of street neighbours | " + ", ".join(f"> {k} {U}: {n(v)}" for k, v in A["outliers"].items()) + " |")
    if "junction" in A:
        j = A["junction"]
        w(f"| junctions not coupled to any storage node | {n(j['uncoupled'])} of {n(j['n'])} ({fd(j['uncoupled_by_net'])}) |")
        w(f"| junction invert above a coupled street | {n(j['invert_above_coupled_storage'])} |")
        if j["rim_minus_street"]:
            w(f"| junction rim (invert + MaxDepth) − coupled street | median {j['rim_minus_street']['50%']:.2f} {U}, "
              f"min {j['rim_minus_street']['min']:.2f} {U} |")
        if j["depth_to_street"]:
            w(f"| coupled street − junction invert (manhole depth) | median {j['depth_to_street']['50%']:.2f} {U}, "
              f"min {j['depth_to_street']['min']:.2f}, max {j['depth_to_street']['max']:.2f} {U} |")
    for r in ["inlet", "overflow"]:
        if f"{r}_crest_below_top" in A:
            a = A[f"{r}_crest_below_top"]
            w(f"| {r} crest below the highest crown/bank at its junction | {n(a['below'])} of {n(a['n'])} "
              f"({n(a['below_05'])} by more than {th[4]:g} {U}) |")
    if A.get("overflow_crest_minus_bank"):
        o = A["overflow_crest_minus_bank"]
        w(f"| overflow crest − channel bank at the junction | median {o['50%']:+.2f} {U}, IQR {o['25%']:+.2f} to {o['75%']:+.2f} |")
    w(f"| degree-1 nodes | {fd(A['degree1'])} |")
    if A["n_other_links"]:
        w(f"| links with role *other* | {fd(A['n_other_links'])} (`tables/other_links.csv`) |")
    w("")
    if A.get("junction", {}).get("rim_minus_street", {}).get("min", 0) > 5 * (1 if U == "m" else 3.28):
        w("Junction rims lie far above the street, so **junctions never flood**: all surcharge returns to the street "
          "through the couplings.")
        w("")
    if A["outfalls"]:
        w("**Outfall links**")
        w("")
        w(md_table(pd.DataFrame(A["outfalls"])))
        w("")
    if has("outliers_map"):
        w(FIGTAG.format("outliers_map"))
        w("")

    # ---------------------------------------------------------------- 9
    w("## 9. Summary of findings")
    w("")
    items = []
    if mi["n"]:
        items.append(f"**{pl(mi['n'], 'disconnected component')}** ({pl(mi['n_nodes'], 'node')}, {pl(mi['n_links'], 'link')}, "
                     f"{pl(mi['n_subcatch'], 'subcatchment')} draining to them): remove them or connect them (§1).")
    if s2["n"]:
        items.append(f"**{n(s2['n'])} storage↔junction links that are not orifices/weirs**: review them, and give them their own edge type (§2).")
    if se.get("n_mismatch"):
        items.append(f"**{n(se['n_mismatch'])} street ends** whose invert differs from the storage invert (§3).")
    if rv["n"]:
        items.append(f"**{pl(rv['n'], 'reversed coupling')}**, {n(rv['n_crest_above_storage'])} with the crest above the street"
                     + (": fix crest and direction (§4)." if rv['n_crest_above_storage'] else "; the crest is consistent, so the direction is only a sign convention (§4)."))
    if cv["pipe"].get("n_neg"):
        items.append(f"**{n(cv['pipe']['n_neg'])} pipe connections ({cv['pipe']['pct_neg']} %) with the crown above the street**, "
                     f"mostly shallow junctions (§5).")
    if cv["channel"].get("n_neg_1"):
        items.append(f"**{n(cv['channel']['n_neg_1'])} channel connections with the bank more than {th[6]:g} {U} above the street** (§5).")
    if "length_above" in R and R["length_above"]:
        LA = pd.DataFrame(R["length_above"])
        for k in ["pipe", "channel"]:
            r0 = LA[(LA.kind == k) & (LA.threshold == th[0])]
            r5 = LA[(LA.kind == k) & (LA.threshold == th[4])]
            if len(r0) and r0.assessed.iloc[0] > 0:
                items.append(f"{k.capitalize()}s above the street surface: {r0.pct_assessed.iloc[0]:.1f} % of the assessed length "
                             f"({r5.pct_assessed.iloc[0]:.1f} % by more than {th[4]:g} {U}) (§7).")
    for k in ["pipe", "channel"]:
        if k in A and A[k]["adverse"]:
            items.append(f"{n(A[k]['adverse'])} {k}s with adverse slope (§8).")
        if k in A and A[k]["below_node"]:
            items.append(f"{n(A[k]['below_node'])} {k} ends with an offset below the node invert (§8).")
    if not items:
        items.append("No geometric inconsistency was found.")
    for it in items:
        w(f"* {it}")
    w("")
    w("## Assumptions")
    w("")
    w("* STORAGE nodes represent the street surface" + (f" (only those matching `{cfg['surface_regex']}`)" if cfg.get("surface_regex") else "")
      + "; JUNCTIONS represent manholes or channel cross-sections. Real storage units (ponds, tanks) should be excluded "
      "with `--surface-regex`.")
    w("* Reversed coupling = FromNode is the junction and ToNode the storage node.")
    w("* Channel bank depth = the lower bank of the main channel after removing the outer glass walls (§5).")
    w("* Links are straight between nodes unless `[VERTICES]` are given.")
    return "\n".join(L)


CSS = """
:root{--fg:#1d2330;--muted:#5b6475;--bg:#ffffff;--panel:#f5f7fa;--line:#dfe3ea;--accent:#1f5fbf}
@media (prefers-color-scheme: dark){:root{--fg:#e6e9ef;--muted:#a3abb9;--bg:#14171c;--panel:#1d2128;--line:#323844;--accent:#7aa7ff}}
html{background:var(--bg)}body{max-width:1180px;margin:0 auto;padding:24px 16px 80px;color:var(--fg);background:var(--bg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
h1{font-size:1.7em;line-height:1.25;margin:.2em 0 .6em}h2{margin-top:2.2em;padding-top:.6em;border-top:1px solid var(--line)}
h3{margin-top:1.6em}a{color:var(--accent)}code{background:var(--panel);padding:1px 4px;border-radius:4px;font-size:.92em}
table{border-collapse:collapse;margin:.8em 0;font-size:13px;display:block;overflow-x:auto;max-width:100%}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}th{background:var(--panel)}
tr:nth-child(even) td{background:color-mix(in srgb,var(--panel) 45%,transparent)}
blockquote{margin:1em 0;padding:.4em 1em;border-left:4px solid var(--accent);background:var(--panel)}
.toc{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 18px;font-size:14px}
.toc ul{margin:.2em 0;padding-left:1.2em}.fig{margin:1em 0 1.6em;border:1px solid var(--line);border-radius:8px;
background:#fff;overflow:hidden}
"""


def build_html(md_text, figs, title):
    body = markdown.markdown(md_text, extensions=["tables", "toc", "sane_lists"], extension_configs={"toc": {"toc_depth": "2-3"}})
    for name, fig in figs.items():
        tag = f"<p>{FIGTAG.format(name)}</p>"
        if tag in body:
            div = fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True, "displaylogo": False})
            body = body.replace(tag, f'<div class="fig">{div}</div>')
    plotly_js = plotly.offline.get_plotlyjs()
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style><script>{plotly_js}</script></head><body>{body}</body></html>")


def md_for_file(md_text):
    """Markdown version: figure placeholders replaced by a pointer to the HTML report."""
    return re.sub(r"\[\[FIG:([^\]]+)\]\]", r"_(interactive figure `\1` in the HTML report)_", md_text)

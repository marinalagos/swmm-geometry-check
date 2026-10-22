# swmm-geometry-check

Checks the **geometric consistency of a dual-drainage SWMM model** and writes a standalone, interactive
`Geometry_consistency_report.html`. In a dual-drainage model, storage nodes represent the street surface and junctions represent the pipe and channel network.

It was built for the Sarandí–Santo Domingo (SSD) model of the PREVENIR project. It works on any SWMM 5.x `.inp`
file and does not rely on element names.

```
python -m swmm_geomcheck path/to/model.inp
```

opens nothing and changes nothing in your model. It writes a folder `model_geometry_check/` next to the `.inp` with:

| file | content |
|---|---|
| `Geometry_consistency_report.html` | the report, with interactive Plotly figures (zoom, rotate, hover). It is a single file with plotly.js embedded, so it opens offline in any browser |
| `Geometry_consistency_report.md` | the same text in Markdown, without the figures |
| `tables/*.csv` | the full tables behind every section (one row per link, connection, component, …) |
| `results.json` | every number quoted in the report |

---

## 1. Installation

Requires Python ≥ 3.9.

```bash
git clone <this repository> swmm-geometry-check
cd swmm-geometry-check
python -m venv .venv
# Windows:   .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies: numpy, pandas, scipy, networkx, plotly, markdown. SWMM itself is not needed, because the `.inp` file is
read directly.

## 2. Usage

```bash
python -m swmm_geomcheck MODEL.inp [-o OUTPUT_FOLDER] [options]
```

| option | default | meaning |
|---|---|---|
| `-o, --out` | `<inp folder>/<inp name>_geometry_check` | output folder |
| `--tin-max-edge` | `400` (m) | street-surface triangles with a longer edge are dropped, so the surface does not bridge areas with no streets. Use about 3–4 × the typical block size |
| `--surface-regex` | all storage nodes | only STORAGE nodes whose name matches this regex form the street surface. Use it when the model also has real storage units (ponds, tanks), e.g. `--surface-regex "^NODO"` |
| `--role ROLE=REGEX` | topology-based | force the role of links whose name matches. Can be repeated, e.g. `--role inlet=^gutter --role overflow=^weir` |
| `--no-tables` | | do not write the CSV tables |

Examples:

```bash
# SSD model
python -m swmm_geomcheck ../data/model_swmmfmt.inp -o ssd_report

# another basin, with detention ponds named POND* that are not street surface
python -m swmm_geomcheck other_basin.inp --surface-regex "^(?!POND)"

# synthetic example shipped with the repository
python -m swmm_geomcheck examples/example_dual.inp
```

On the SSD model (31,737 nodes, 53,184 links) a run takes about 10–20 s, and the HTML report is about 30 MB. Most of
that is the 3D surface.

## 3. What the model must look like

The checks assume the **dual-drainage (1D/1D) representation**:

* **STORAGE** nodes = street surface (city blocks / street nodes);
* **JUNCTION** nodes = manholes and channel cross-sections;
* surface and sub-surface layers are coupled by **orifices** (street inlets) and **weirs** (street ↔ channel overflow).

Links get a *role* from the node types at their ends and from their cross-section. **Names are not used**, unless you
force roles with `--role`:

| role | rule |
|---|---|
| `street` | CONDUIT with at least one STORAGE end |
| `pipe` | CONDUIT between junctions with a closed section (CIRCULAR, RECT_CLOSED, …) |
| `channel` | CONDUIT between junctions with an open section (IRREGULAR, RECT_OPEN, TRAPEZOIDAL, TRIANGULAR, PARABOLIC, POWER) |
| `inlet` | ORIFICE between a STORAGE node and a JUNCTION (either direction) |
| `overflow` | WEIR between a STORAGE node and a JUNCTION (either direction) |
| `outfall_link` | any link touching an OUTFALL |
| `other` | pumps, outlets, orifices/weirs between junctions, … (counted, listed in `tables/other_links.csv`) |

The report starts with the role table, so check it first. If the model uses a different convention (for example,
junctions for the street surface), the roles will show it. A model without STORAGE nodes is still processed: the
connectivity and additional checks run, and the surface checks are reported as not applicable.

Model features handled:

* `LINK_OFFSETS = DEPTH` (the SWMM default) or `ELEVATION`, including `*` offsets. Everything is converted to absolute
  elevations;
* SI or US units (from `FLOW_UNITS`). Thresholds are converted, and the report uses m or ft;
* `[VERTICES]` (links follow their polyline in the surface checks);
* IRREGULAR (HEC-2 transects) and regular shapes;
* missing coordinates (the spatial checks are skipped);
* undefined nodes (the links are dropped, with a warning in the report).

## 4. What the report checks

| § | check | main table |
|---|---|---|
| Roles | how many links of each role, by SWMM type and end-node types | `link_roles.csv` |
| 1 | **Disconnected subgraphs**: size, content, distance and elevation gap to the main graph, subcatchments draining to them. Components raised far above their surroundings (> 10 m) are flagged as probably switched-off elements. Also the connectivity of the surface layer alone and of the sub-surface layer alone (pipe networks with no outfall) | `components.csv` |
| 2 | **Storage ↔ junction links that are not orifices/weirs** (e.g. streets ending on a channel node): direction, inverts vs both nodes, slope | `storage_junction_noncoupling.csv` |
| 3 | **Storage invert vs street-end inverts**: every street end on a storage node must have the node's invert. Builds the **street surface**: a Delaunay TIN of the storage inverts | `street_end_mismatch.csv` |
| 4 | **Couplings oriented junction → storage**: crest vs street, sibling couplings, and whether the direction matters in SWMM | `reversed_couplings.csv` |
| 5 | **Pipe crowns / channel banks vs the street nodes they are coupled to**, one row per connection (pipe/channel end × inlet/overflow × storage node). Ends without a coupling are not assessed. The bank depth of IRREGULAR channels is taken from the transect (main channel, glass walls removed) | `connections.csv`, `transect_banks.csv` |
| 6 | **3D view** of the street surface with pipe crowns and channel banks | – |
| 7 | **Intersections** of crown/bank lines with the surface: per-link class (below / crosses / above / outside), and the **share of the network length above the surface for several thresholds** (only the part of a link that is above counts). §7.1 of the report describes the method in detail | `intersections.csv`, `length_above_surface.csv` |
| 8 | **Additional checks**: offsets below node inverts, drops, adverse/steep slopes, SWMM length vs plan length, self-loops, parallel links, storage-invert outliers vs neighbours, uncoupled junctions, manhole depth and rim, coupling crest vs crowns, outfall links | `offsets_below_node.csv`, `adverse_or_steep_links.csv`, `storage_elev_outliers.csv`, … |
| 9 | **Summary of findings** | – |

## 5. Interpreting the results

The report text is generated automatically from the numbers. It flags what is unusual, but **it cannot know the
design intent**. Read every finding together with the map figures and, ideally, a DEM. In particular:

* the "street surface" is the **street invert** (gutter line), interpolated linearly between street nodes. It is not the
  terrain. Exceedances of a few decimetres are within its uncertainty;
* a channel bank above the coupled street can be a real levee;
* a component raised by about 100 m is almost always a switched-off element, but check it before deleting it.

## 6. Repository layout

```
swmm_geomcheck/
  inp.py        .inp reader (nodes, links, offsets → elevations, sections, transects, vertices, subcatchments)
  checks.py     link roles and all checks; returns numbers, tables and Plotly figures
  report.py     report text (Markdown) and standalone HTML
  __main__.py   command line
examples/
  make_example.py, example_dual.inp   small synthetic dual-drainage model (DEPTH offsets, vertices, a reversed
                                      inlet, a too-tall pipe, a raised isolated element)
tests/
  test_example.py                     smoke test: python tests/test_example.py (or pytest)
```

To add a check: compute it in `checks.run` (store numbers in `R`, tables in `TAB`, figures in `FIG`), then write its
section in `report.build_markdown`. A figure is placed in the text with the line `[[FIG:<name>]]`.

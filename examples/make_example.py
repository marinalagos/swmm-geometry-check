"""Writes examples/example_dual.inp: a small synthetic dual-drainage model used to test the tool.

5 x 5 street grid (storage nodes, 100 m spacing, sloping to the east), a pipe line under the middle street with
inlets, a trapezoidal channel on the east side with overflow weirs, one reversed inlet, one pipe whose crown is
above the street, one isolated street segment raised by 100 m, link vertices, and LINK_OFFSETS = DEPTH.
"""
from pathlib import Path

L = []
w = L.append
w("[TITLE]\nSynthetic dual-drainage example\n")
w("[OPTIONS]\nFLOW_UNITS CMS\nFLOW_ROUTING DYNWAVE\nLINK_OFFSETS DEPTH\n")
elev = lambda i, j: 10.0 - 0.2 * i + 0.05 * j  # noqa: E731  (i: x index, j: y index)
jun, sto, con, ori, wei, xs, co, ver = [], [], [], [], [], [], [], []
for i in range(5):
    for j in range(5):
        s = f"S{i}{j}"
        sto.append(f"{s} {elev(i, j):.3f} 5 0 TABULAR SC 1000 0")
        co.append(f"{s} {i * 100} {j * 100}")
for i in range(5):            # streets along x and along y; DEPTH offsets = 0 at both ends
    for j in range(5):
        if i < 4:
            con.append(f"SX{i}{j} S{i}{j} S{i + 1}{j} 100 0.02 0 0"); xs.append(f"SX{i}{j} IRREGULAR ST10")
        if j < 4:
            con.append(f"SY{i}{j} S{i}{j} S{i}{j + 1} 100 0.02 0 0"); xs.append(f"SY{i}{j} IRREGULAR ST10")
for i in range(4):            # pipe line under row j=2, manholes 1.5 m below the street
    jun.append(f"J{i} {elev(i, 2) - 1.5:.3f} 5 0 0 0"); co.append(f"J{i} {i * 100 + 5} 205")
    ori.append(f"I{i} S{i}2 J{i} SIDE 0 0.65 NO 0" if i != 1 else f"I{i} J{i} S{i}2 SIDE 1.5 0.65 NO 0")  # I1 reversed
    xs.append(f"I{i} RECT_CLOSED 0.15 1.0 0 0")
for i in range(3):
    d = 1.8 if i == 2 else 0.8  # P2 is too tall for its 1.5 m deep manhole
    con.append(f"P{i} J{i} J{i + 1} 100 0.015 0 0"); xs.append(f"P{i} CIRCULAR {d} 0 0 0 1")
ver.append("P0 50 210")
for j in range(5):            # channel along x = 450, bank at street level, connected by weirs
    jun.append(f"C{j} {elev(4, j) - 2.0:.3f} 5 0 0 0"); co.append(f"C{j} 450 {j * 100}")
    wei.append(f"W{j} S4{j} C{j} SIDEFLOW 0.05 1.6 NO 0 0"); xs.append(f"W{j} RECT_OPEN 1 10 0 0")
for j in range(4):
    con.append(f"CH{j} C{j + 1} C{j} 100 0.03 0 0"); xs.append(f"CH{j} TRAPEZOIDAL 2.0 4 1 1 1")
con.append("P3 J3 C2 50 0.015 0 0"); xs.append("P3 CIRCULAR 0.8 0 0 0 1")
w("[JUNCTIONS]\n" + "\n".join(jun) + "\nZ0 120.000 5 0 0 0\nZ1 120.000 5 0 0 0\n")
w(f"[OUTFALLS]\nOUT {elev(4, 0) - 2.2:.3f} FREE NO\n")
w("[STORAGE]\n" + "\n".join(sto) + "\n")
con.append("OL C0 OUT 20 0.03 0 0"); xs.append("OL TRAPEZOIDAL 2.0 4 1 1 1")
con.append("ZZ Z0 Z1 50 0.015 0 0"); xs.append("ZZ CIRCULAR 0.5 0 0 0 1")  # isolated, raised by 100 m
w("[CONDUITS]\n" + "\n".join(con) + "\n")
w("[ORIFICES]\n" + "\n".join(ori) + "\n")
w("[WEIRS]\n" + "\n".join(wei) + "\n")
w("[XSECTIONS]\n" + "\n".join(xs) + "\n")
w("[TRANSECTS]\nNC 0.02 0.02 0.02\nX1 ST10 7 -5 5 0 0 0 0 0\n"
  "GR 3 -10 0.35 -10 0.15 -5 0 -5 0.1 0\nGR 0 5 0.15 5 0.35 10 3 10\n")
w("[CURVES]\nSC STORAGE 0 1000\nSC 5 1000\n")
w("[COORDINATES]\n" + "\n".join(co) + "\nZ0 600 600\nZ1 650 600\nOUT 470 -20\n")
w("[VERTICES]\n" + "\n".join(ver) + "\n")
Path(__file__).with_name("example_dual.inp").write_text("\n".join(L))
print("written example_dual.inp")

"""
Saudi 360 — convert the published TopoJSON to GeoJSON for the map.

    python build_map.py     ->  site/geo/sau.geo.json

The map library takes GeoJSON; the published administrative boundaries come as
TopoJSON. Converting here rather than in the browser keeps the page free of a
second mapping library.

No coordinate is invented: every point comes from the published topology's arcs,
decoded with its own quantisation transform.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "site" / "geo" / "sau.topo.json"
OUT = ROOT / "site" / "geo" / "sau.geo.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("map")


def decode_arcs(topo: dict) -> list[list[list[float]]]:
    """Arcs as absolute lon/lat, undoing delta encoding and quantisation."""
    tr = topo.get("transform")
    out = []
    for arc in topo["arcs"]:
        points, x, y = [], 0, 0
        for dx, dy in arc:
            x += dx
            y += dy
            if tr:
                points.append([x * tr["scale"][0] + tr["translate"][0],
                               y * tr["scale"][1] + tr["translate"][1]])
            else:
                points.append([x, y])
        out.append(points)
    return out


def ring(indices: list[int], arcs: list) -> list[list[float]]:
    """Stitch a ring from arc indices; a negative index means that arc reversed."""
    coords: list[list[float]] = []
    for idx in indices:
        arc = arcs[~idx][::-1] if idx < 0 else arcs[idx]
        coords.extend(arc[1:] if coords else arc)
    return coords


def main() -> int:
    if not SRC.exists():
        log.error("missing %s", SRC)
        return 1
    topo = json.loads(SRC.read_text("utf-8"))
    arcs = decode_arcs(topo)
    key = next(iter(topo["objects"]))

    features = []
    for geom in topo["objects"][key]["geometries"]:
        gid = geom.get("id")
        name = (geom.get("properties") or {}).get("name")
        if not gid or gid == "-99" or not name:
            continue                      # the topology's unnamed placeholder
        if geom["type"] == "Polygon":
            coords = [ring(r, arcs) for r in geom["arcs"]]
        elif geom["type"] == "MultiPolygon":
            coords = [[ring(r, arcs) for r in poly] for poly in geom["arcs"]]
        else:
            continue
        features.append({
            "type": "Feature",
            "id": gid,
            # The map library matches features by `properties.name`, so the
            # region id is carried there and the human name kept beside it.
            "properties": {"name": gid, "label": name},
            "geometry": {"type": geom["type"], "coordinates": coords},
        })

    fc = {"type": "FeatureCollection", "features": features}
    OUT.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")

    lons = []
    lats = []
    def walk(c):
        if isinstance(c[0], (int, float)):
            lons.append(c[0]); lats.append(c[1])
        else:
            for x in c:
                walk(x)
    for f in features:
        walk(f["geometry"]["coordinates"])
    log.info("%d regions -> %s (%.0f KB)", len(features), OUT, OUT.stat().st_size / 1024)
    log.info("bounds lon %.2f..%.2f  lat %.2f..%.2f", min(lons), max(lons), min(lats), max(lats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import math

import h3

EARTH_RADIUS_KM = 6371.0088


def cell(lat, lon, res=7):
    if lat is None or lon is None:
        return None
    return h3.latlng_to_cell(lat, lon, res)


def neighbors(h3_cell, rings=1):
    return set(h3.grid_disk(h3_cell, rings))


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def polygons(geometry):
    """Yield each polygon (list of rings, [lon, lat] pairs) in a GeoJSON geometry."""
    if not geometry:
        return
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        yield coords
    elif kind == "MultiPolygon":
        yield from coords
    elif kind == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            yield from polygons(g)


def _in_ring(lat, lon, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def contains(geometry, lat, lon):
    for poly in polygons(geometry):
        if not poly or not _in_ring(lat, lon, poly[0]):
            continue
        if any(_in_ring(lat, lon, hole) for hole in poly[1:]):
            continue
        return True
    return False


def bbox(geometry):
    """(min_lat, min_lon, max_lat, max_lon) of a polygonal geometry, or None."""
    lats, lons = [], []
    for poly in polygons(geometry):
        for ring in poly[:1]:
            for x, y, *_ in ring:
                lons.append(x)
                lats.append(y)
    if not lats:
        return None
    return min(lats), min(lons), max(lats), max(lons)


def merge(geometries):
    """Combine polygon geometries into one MultiPolygon (no dissolve)."""
    parts = []
    for g in geometries:
        parts.extend(polygons(g))
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "Polygon", "coordinates": parts[0]}
    return {"type": "MultiPolygon", "coordinates": parts}


def round_geometry(geometry, ndigits=5):
    def rnd(value):
        if isinstance(value, (int, float)):
            return round(value, ndigits)
        return [rnd(v) for v in value]
    return {"type": geometry["type"], "coordinates": rnd(geometry["coordinates"])}


def _simplify_ring(points, tolerance):
    if len(points) < 5:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        ax, ay = points[start][0], points[start][1]
        bx, by = points[end][0], points[end][1]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, index = 0.0, None
        for i in range(start + 1, end):
            px, py = points[i][0], points[i][1]
            if norm == 0:
                dist = math.hypot(px - ax, py - ay)
            else:
                dist = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if dist > worst:
                worst, index = dist, i
        if index is not None and worst > tolerance:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    out = [p for p, k in zip(points, keep) if k]
    return out if len(out) >= 4 else points


def simplify(geometry, tolerance=0.01):
    """Douglas-Peucker simplification for display only; never used for containment."""
    parts = [[_simplify_ring(ring, tolerance) for ring in poly] for poly in polygons(geometry)]
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "Polygon", "coordinates": parts[0]}
    return {"type": "MultiPolygon", "coordinates": parts}

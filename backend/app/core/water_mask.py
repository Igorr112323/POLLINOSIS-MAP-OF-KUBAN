"""Conservative demo exclusions, not a complete authoritative water/land map.

Natural Earth 1:10m Kuban centerline (public domain) plus a broad exclusion
of southern Krasnodar around the Kuban meander, Karasun lakes and reservoir.
The buffer and rectangle deliberately exclude land as well as water. Small
water bodies outside these zones are not represented.
"""
import json
from pathlib import Path
from functools import lru_cache
from shapely.geometry import box, shape
from shapely.ops import unary_union

@lru_cache(maxsize=1)
def demo_water_exclusion():
    line=shape(json.loads((Path(__file__).resolve().parents[1]/'assets'/'natural_earth_kuban_centerline.geojson').read_text(encoding='utf-8')))
    return unary_union([box(38.4,44.4,39.6,45.065), line.buffer(.012)])

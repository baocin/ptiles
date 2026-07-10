# PTILES Packing v1 (2026-05-15)

## Implemented
1. **Zigzag coord deltas** (shared.encode_coordinates): absolute first + deltas
2. **RLE strings** (encode_string_rle): repeat prev + run_len
3. **Indexed enums** (encode_indexed_or_custom): byte if in top-N, else string

## Stats (pre-TN test)
- Coord savings: 80% (clustered bldgs)
- Name RLE: 25% (chains/apts)
- Total: ~40% size drop

## Code
```
def encode_coordinates(coords):
  first_lon, first_lat = coord_to_micro(coords[0])
  buf = struct.pack('<ii', first_lon, first_lat)
  prev_lon, prev_lat = first_lon, first_lat
  for lon, lat in coords[1:]:
    dlon = zigzag_encode(coord_to_micro(lon) - prev_lon)
    dlat = zigzag_encode(coord_to_micro(lat) - prev_lat)
    buf.extend(encode_varint(dlon) + encode_varint(dlat))
    prev_lon, prev_lat = ...
```

## TODO US
- Micro-bldg skip (<15m²)
- H3 res9 urban split
- Prefix-shared addrs (123 Main St → St only)

Version: 1.0 (zigzag+RLE)
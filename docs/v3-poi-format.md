# PTILESBv3 — Unified POI standalone point layer

**Magic:** `PTILESB\0` (unchanged)
**Version:** 3

## Record Format (v3)

Same header/index/dictionary layout as v1/v2. The record body changes:

```
u64 unified_id              — zigzag-varint, delta from previous
i32 lon_micro               — absolute centroid (degrees × 100,000)
i32 lat_micro               — absolute centroid
u16_str name                — business name (required)
u8 category_idx             — indexed taxonomy, 0 = missing
u8 flags
  0x01: phone               (u8_str)
  0x02: website             (u8_str)
  0x04: address             (u16_str: freeform address)
  0x08: brand               (u8_str)
  0x10: operating_status    (2-bit: 0=open, 1=closed, 2=temporarily)
  0x20: emails              (u8_str, semicolon-joined)
  0x40: socials             (u8_str, semicolon-joined)
  0x80: HAS_EXTENDED_ATTRS  — if set, a 2-byte extended flags field follows
       after all the optional bytes above (phone, website, address, brand, email, social)

[EXTENDED_ATTRS, only when flags & 0x80]
u16 ext_flags
  0x0001: source_type       (u8 — see Source enum)
  0x0002: source_id         (u16_str — original source ID string)
  0x0004: confidence        (u8 — 0-100, 0 = unset)
  0x0008: unified_id_alt    (u64 — alternate unified_id for cross-source merge)
```

## Source enum

```
0 = osm         — OSM node ID
1 = overture    — Overture Maps UUID
2 = foursquare  — Foursquare Places V2 hex ID
3 = custom      — u16_str follows
```

## unified_id strategy

Every record needs a deterministic u64 that can be the _same_ across Foursquare and Overture when they represent the same business.

- **Overture records** that reference an OSM node: use the OSM node id directly
- **Overture records** with no OSM reference: hash SHA-256(`overture:{uuid}`) → first 8 bytes
- **Foursquare records**: hash SHA-256(`foursquare:{hex_id}`) → first 8 bytes
- **Cross-source match**: the `unified_id_alt` field stores the other source's unified_id. Records that are deduped share a primary unified_id (the Foursquare-derived one), and the Overture copy stores the same unified_id with the Foursquare unified_id in `unified_id_alt`.

## Backward compatibility

Old readers see `version >= 1`, read the header fine. They parse the v3 record up through the optional fields (phone, website, address, brand, email, social). When they hit `flags & 0x80`, they don't know about `HAS_EXTENDED_ATTRS` — but they've already consumed all the fields they know about, and the extended attrs section is at the _end_ of the record. So the old reader finishes its parse, sees nothing extra, and the remaining bytes are unread (gracefully skipped because each record is length-delimited). The category and name fields are at the same offsets.

This means v3 files are **fully readable by v1/v2 consumers** — the only cost is the `unified_id` field replaces `osm_id` (both are u64, same semantics). The `extended_attrs` trailing bytes are harmless.

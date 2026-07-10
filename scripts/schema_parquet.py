#!/usr/bin/env python3
"""
Build a unified state Parquet file from PBF + existing PTILES + POI data.

Output schema (one Parquet file per state):

TN.parquet

-- roads (linestring, one row per road segment, NOT H3-split)
CREATE TABLE roads (
    osm_id BIGINT,
    highway TEXT,            -- motorway, trunk, primary, secondary, etc.
    name TEXT,
    ref TEXT,
    oneway BOOLEAN,
    maxspeed SMALLINT,
    lanes SMALLINT,
    surface TEXT,
    bridge BOOLEAN,
    tunnel BOOLEAN,
    geometry GEOMETRY(LINESTRING)  -- WKB
);

-- water (polygon + linestring)
CREATE TABLE water (
    osm_id BIGINT,
    water_type TEXT,         -- lake, reservoir, river, stream, etc.
    name TEXT,
    width INT,
    geometry GEOMETRY
);

-- buildings (polygon)
CREATE TABLE buildings (
    osm_id BIGINT,
    btype TEXT,              -- house, residential, commercial, etc.
    use_class TEXT,          -- unknown, residential, commercial, industrial
    height_tier TEXT,        -- unknown, 1-2fl, 3-5fl, 6+fl
    height_m REAL,
    name TEXT,
    category TEXT,
    geometry GEOMETRY(POLYGON)
);

-- business (point)
CREATE TABLE business (
    unified_id BIGINT,
    name TEXT,
    brand TEXT,
    category TEXT,
    phone TEXT,
    website TEXT,
    address TEXT,
    lat DOUBLE,
    lon DOUBLE,
    source TEXT,             -- OSM, Overture, Foursquare
    source_id TEXT,
    confidence SMALLINT
);

-- parks (polygon)
CREATE TABLE parks (
    osm_id BIGINT,
    park_type TEXT,
    name TEXT,
    geometry GEOMETRY(POLYGON)
);

-- rail (linestring + point)
CREATE TABLE rail (
    osm_id BIGINT,
    rail_type TEXT,          -- rail, subway, light_rail, tram, station, etc.
    name TEXT,
    geom_type TEXT,          -- line or station
    geometry GEOMETRY
);

-- places (point)
CREATE TABLE places (
    osm_id BIGINT,
    place_type TEXT,         -- city, town, village, etc.
    name TEXT,
    alt_name TEXT,
    population INT,
    admin_level SMALLINT,
    lat DOUBLE,
    lon DOUBLE
);
"""

print("Schema designed — ready to build")

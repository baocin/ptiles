//! PTiles v7: Multi-layer geospatial binary format reader.
//!
//! PTiles v7 extends the v6 building-footprint format into a 7-layer container:
//!
//! | Layer | Geometry   | Description |
//! |-------|------------|-------------|
//! | 0     | Polygon    | Buildings   |
//! | 1     | Linestring | Roads       |
//! | 2     | Point      | Places      |
//! | 3     | Polygon    | Admin       |
//! | 4     | Polygon    | Water       |
//! | 5     | Linestring | Rail        |
//! | 6     | Polygon    | Parks       |
//!
//! # Example
//!
//! ```no_run
//! use ptiles_v7::PtilesReader;
//!
//! let reader = PtilesReader::open("US.ptiles.v7").unwrap();
//! let header = reader.header();
//! println!("Layers: {}", header.layer_count);
//! println!("Total features: {}", header.total_poi_count);
//! ```

pub mod header;
pub mod index;
pub mod layer;
pub mod reader;
pub mod records;
pub mod varint;

pub use header::Header;
pub use index::SpatialIndex;
pub use layer::{LayerDir, LayerDirEntry, LayerType};
pub use reader::PtilesReader;

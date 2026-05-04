//! PTiles v7 file reader.
//!
//! Provides a high-level API for opening and querying PTiles v7 files.

use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::Path;
use thiserror::Error;

use crate::header::{Header, HeaderError};
use crate::index::{IndexError, SpatialIndex};
use crate::layer::{LayerDir, LayerDirError, LayerType};
use crate::records::{
    admin, buildings, parks, places, rail, roads, water,
};

/// Feature enum wrapping all 7 layer record types.
#[derive(Debug, Clone)]
pub enum Feature {
    Building(buildings::Building),
    Road(roads::Road),
    Place(places::Place),
    Admin(admin::AdminBoundary),
    Water(water::WaterBody),
    Rail(rail::Railway),
    Park(parks::Park),
}

/// Error type for the PTiles reader.
#[derive(Error, Debug)]
pub enum PtilesError {
    #[error("header error: {0}")]
    Header(#[from] HeaderError),

    #[error("layer directory error: {0}")]
    LayerDir(#[from] LayerDirError),

    #[error("index error: {0}")]
    Index(#[from] IndexError),

    #[error("I/O error: {0}")]
    Io(#[from] io::Error),

    #[error("layer {0:?} not found in file")]
    LayerNotFound(LayerType),

    #[error("H3 cell not found in index for layer {0:?}")]
    CellNotFound(LayerType),

    #[error("zstd decompression error: {0}")]
    Zstd(String),

    #[error("record too short: expected {expected} bytes, got {actual}")]
    RecordTooShort { expected: usize, actual: usize },
}

/// The main PTiles v7 file reader.
///
/// Opens a `.ptiles` v7 file and provides methods to query features by
/// H3 cell and layer.
pub struct PtilesReader {
    file: File,
    header: Header,
    layer_dir: LayerDir,
    index: SpatialIndex,
    /// Cached dictionary bytes for zstd decompression.
    dict: Vec<u8>,
}

impl PtilesReader {
    /// Open a PTiles v7 file.
    ///
    /// Reads and validates the header, layer directory, spatial index,
    /// and zstd dictionary.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, PtilesError> {
        let mut file = File::open(path)?;

        // Read header
        let header = Header::read(&mut file)?;

        // Read zstd dictionary
        file.seek(SeekFrom::Start(header.dict_offset))?;
        let mut dict = vec![0u8; header.dict_length as usize];
        file.read_exact(&mut dict)?;

        // Read layer directory
        file.seek(SeekFrom::Start(header.layer_dir_offset))?;
        let layer_dir = LayerDir::read(&mut file)?;

        // Compute total index entry count from layer directory
        let total_entries: usize = layer_dir
            .entries
            .iter()
            .map(|e| e.index_count as usize)
            .sum();

        // Read spatial index
        file.seek(SeekFrom::Start(header.index_offset))?;
        let index = SpatialIndex::read(&mut file, total_entries)?;

        Ok(PtilesReader {
            file,
            header,
            layer_dir,
            index,
            dict,
        })
    }

    /// Get a reference to the file header.
    pub fn header(&self) -> &Header {
        &self.header
    }

    /// Get a reference to the layer directory.
    pub fn layer_dir(&self) -> &LayerDir {
        &self.layer_dir
    }

    /// Get a reference to the spatial index.
    pub fn index(&self) -> &SpatialIndex {
        &self.index
    }

    /// Query features in a specific H3 cell for a specific layer.
    ///
    /// Returns all features in the block. Decoding stops at the first
    /// unrecoverable parse error, returning what was decoded so far.
    pub fn query_cell(
        &mut self,
        layer: LayerType,
        h3_cell: u64,
    ) -> Result<Vec<Feature>, PtilesError> {
        let entry = self
            .index
            .find(layer, h3_cell)
            .ok_or(PtilesError::CellNotFound(layer))?;

        // Read the compressed block
        self.file.seek(SeekFrom::Start(entry.block_offset))?;
        let mut compressed = vec![0u8; entry.block_length as usize];
        self.file.read_exact(&mut compressed)?;

        // Decompress with shared dictionary
        let mut decoder = zstd::Decoder::with_dictionary(&compressed[..], &self.dict[..])
            .map_err(|e| PtilesError::Zstd(e.to_string()))?;
        let mut decompressed = Vec::new();
        decoder
            .read_to_end(&mut decompressed)
            .map_err(|e| PtilesError::Zstd(e.to_string()))?;

        // Decode records
        self.decode_block(layer, &decompressed)
    }

    /// Query features across all layers for a given H3 cell.
    ///
    /// Returns a `Vec<Feature>` containing all features in that cell
    /// across every layer, in layer order (0-6).
    pub fn query_cell_all(&mut self, h3_cell: u64) -> Result<Vec<Feature>, PtilesError> {
        let mut all_features = Vec::new();
        for layer_idx in 0..7u8 {
            let layer = LayerType::from_u8(layer_idx).unwrap();
            if let Ok(features) = self.query_cell(layer, h3_cell) {
                all_features.extend(features);
            }
        }
        Ok(all_features)
    }

    /// Decode a decompressed block into a vector of features.
    fn decode_block(&self, layer: LayerType, data: &[u8]) -> Result<Vec<Feature>, PtilesError> {
        let _poi_count = data.len(); // approximate — each record has its own length prefix
        let mut features = Vec::new();
        let mut pos: usize = 0;
        let mut prev_osm_id: u64 = 0;

        while pos + 4 <= data.len() {
            // Read record length prefix
            let record_len = u32::from_le_bytes([
                data[pos],
                data[pos + 1],
                data[pos + 2],
                data[pos + 3],
            ]) as usize;
            pos += 4;

            if pos + record_len > data.len() {
                // Truncated record; stop decoding
                break;
            }

            let feature = match layer {
                LayerType::Buildings => {
                    let (b, _) = buildings::decode_building(data, pos, prev_osm_id);
                    prev_osm_id = b.osm_id;
                    Feature::Building(b)
                }
                LayerType::Roads => {
                    let (r, _) = roads::decode_road(data, pos, prev_osm_id);
                    prev_osm_id = r.osm_id;
                    Feature::Road(r)
                }
                LayerType::Places => {
                    let (p, _) = places::decode_place(data, pos, prev_osm_id);
                    prev_osm_id = p.osm_id;
                    Feature::Place(p)
                }
                LayerType::Admin => {
                    let (a, _) = admin::decode_admin(data, pos, prev_osm_id);
                    prev_osm_id = a.osm_id;
                    Feature::Admin(a)
                }
                LayerType::Water => {
                    let (w, _) = water::decode_water(data, pos, prev_osm_id);
                    prev_osm_id = w.osm_id;
                    Feature::Water(w)
                }
                LayerType::Rail => {
                    let (r, _) = rail::decode_rail(data, pos, prev_osm_id);
                    prev_osm_id = r.osm_id;
                    Feature::Rail(r)
                }
                LayerType::Parks => {
                    let (p, _) = parks::decode_park(data, pos, prev_osm_id);
                    prev_osm_id = p.osm_id;
                    Feature::Park(p)
                }
            };

            features.push(feature);
            pos += record_len;
        }

        Ok(features)
    }
}

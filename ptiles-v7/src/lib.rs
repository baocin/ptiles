// PTiles v7 Reader
//
// Multi-layer timeline spatial data format.
// Layers: buildings, roads, places, admin, water, rail, parks.
//
// File structure:
//   Header (256 bytes) → Zstd Dictionary → Layer Directory → Spatial Indexes → Data Blocks
//
// Magic: "PTILEST\x00" (T = timeline)

pub mod header;
pub mod layer_dir;
pub mod spatial_index;
pub mod layers;
pub mod varint;

use std::io::{Read, Seek, SeekFrom};

use header::Header;
use layer_dir::{LayerDirectory, LayerEntry, LayerType};
use spatial_index::SpatialIndex;
use layers::building::BuildingRecord;
use layers::road::RoadRecord;
use layers::place::PlaceRecord;
use layers::admin::AdminRecord;
use layers::water::WaterRecord;
use layers::rail::RailRecord;
use layers::park::ParkRecord;

/// A decoded record from any v7 layer.
#[derive(Debug, Clone)]
pub enum Record {
    Building(BuildingRecord),
    Road(RoadRecord),
    Place(PlaceRecord),
    Admin(AdminRecord),
    Water(WaterRecord),
    Rail(RailRecord),
    Park(ParkRecord),
}

/// Main reader for PTiles v7 files.
pub struct PtilesReader<R: Read + Seek> {
    reader: R,
    pub header: Header,
    dict: Vec<u8>,
    layer_dir: LayerDirectory,
}

impl<R: Read + Seek> PtilesReader<R> {
    /// Open a PTiles v7 file and parse header, dictionary, and layer directory.
    pub fn open(mut reader: R) -> std::io::Result<Self> {
        let header = Header::read(&mut reader)?;

        // Read zstd dictionary
        reader.seek(SeekFrom::Start(header.dict_offset))?;
        let mut dict = vec![0u8; header.dict_length as usize];
        reader.read_exact(&mut dict)?;

        // Read layer directory
        reader.seek(SeekFrom::Start(header.layer_dir_offset))?;
        let mut layer_dir_bytes = vec![0u8; header.layer_dir_length as usize];
        reader.read_exact(&mut layer_dir_bytes)?;
        let layer_dir = LayerDirectory::decode(&layer_dir_bytes);

        Ok(PtilesReader {
            reader,
            header,
            dict,
            layer_dir,
        })
    }

    /// Return a reference to the layer directory.
    pub fn layer_dir(&self) -> &LayerDirectory {
        &self.layer_dir
    }

    /// Return the number of layers.
    pub fn layer_count(&self) -> u8 {
        self.layer_dir.layer_count
    }

    /// Get a layer entry by type.
    pub fn get_layer(&self, layer_type: LayerType) -> Option<&LayerEntry> {
        self.layer_dir.layers.iter().find(|l| l.layer_type == layer_type)
    }

    /// Read the spatial index for a given layer.
    pub fn read_spatial_index(&mut self, layer: &LayerEntry) -> std::io::Result<SpatialIndex> {
        self.reader.seek(SeekFrom::Start(layer.index_offset))?;
        SpatialIndex::read(&mut self.reader)
    }

    /// Return a reference to the zstd dictionary (can be shared across decompressions).
    pub fn dict(&self) -> &[u8] {
        &self.dict
    }

    /// Decode a Raw block of compressed data for a layer.
    pub fn decompress_block(&self, compressed: &[u8]) -> std::io::Result<Vec<u8>> {
        let mut decoder = zstd::stream::Decoder::with_dictionary(compressed, &self.dict[..])?;
        let mut decompressed = Vec::new();
        decoder.read_to_end(&mut decompressed)?;
        Ok(decompressed)
    }

    /// Read a compressed block from the file by offset and length.
    pub fn read_raw_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<u8>> {
        self.reader.seek(SeekFrom::Start(offset))?;
        let mut buf = vec![0u8; length as usize];
        self.reader.read_exact(&mut buf)?;
        Ok(buf)
    }

    /// Read, decompress, and decode buildings from a block.
    pub fn read_buildings_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<BuildingRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(BuildingRecord::decode_block(&decompressed))
    }

    /// Read, decompress, and decode roads from a block.
    pub fn read_roads_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<RoadRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(RoadRecord::decode_block(&decompressed))
    }

    /// Read, decompress, and decode places from a block.
    pub fn read_places_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<PlaceRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(PlaceRecord::decode_block(&decompressed))
    }

    /// Read, decompress, and decode admin boundaries from a block.
    pub fn read_admin_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<AdminRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(AdminRecord::decode_block(&decompressed))
    }

    /// Read, decompress, and decode water bodies from a block.
    pub fn read_water_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<WaterRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(WaterRecord::decode_block(&decompressed))
    }

    /// Read, decompress, and decode rail lines from a block.
    pub fn read_rail_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<RailRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(RailRecord::decode_block(&decompressed))
    }

    /// Read, decompress, and decode parks from a block.
    pub fn read_parks_block(&mut self, offset: u64, length: u32) -> std::io::Result<Vec<ParkRecord>> {
        let raw = self.read_raw_block(offset, length)?;
        let decompressed = self.decompress_block(&raw)?;
        Ok(ParkRecord::decode_block(&decompressed))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_building_record_size() {
        // A simple building: 4 vertices square, no optional fields
        let b = BuildingRecord {
            osm_id: 12345,
            geometry: vec![
                (0.0, 0.0),
                (0.001, 0.0),
                (0.001, 0.001),
                (0.0, 0.001),
            ],
            centroid_lat: 0.0005,
            centroid_lon: 0.0005,
            building_type: "yes".to_string(),
            name: None,
            category: None,
            name_source: None,
            poi_osm_id: None,
            height_m: None,
        };
        let encoded = BuildingRecord::encode_block(&[b.clone()]);
        let decoded = BuildingRecord::decode_block(&encoded);
        assert_eq!(decoded.len(), 1);
        assert_eq!(decoded[0].osm_id, 12345);
    }
}

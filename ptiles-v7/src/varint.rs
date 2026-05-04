//! Protobuf-style variable integer encoding.
//!
//! Used for OSM ID deltas and coordinate deltas throughout the PTiles format.
//! Same encoding as v6: 7 bits per byte, MSB as continuation flag, little-endian.

/// Decode an unsigned varint from a byte slice starting at `offset`.
///
/// Returns `(value, bytes_consumed)`. The value is the decoded unsigned integer.
///
/// # Panics
///
/// Panics if the varint is truncated (missing continuation byte in available data).
pub fn decode_varint(data: &[u8], offset: usize) -> (u64, usize) {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    let mut consumed: usize = 0;
    loop {
        let b = data[offset + consumed];
        result |= ((b & 0x7F) as u64) << shift;
        consumed += 1;
        if (b & 0x80) == 0 {
            break;
        }
        shift += 7;
    }
    (result, consumed)
}

/// Decode a signed integer from a zigzag-encoded unsigned varint.
///
/// Zigzag encoding maps small negative numbers to small unsigned numbers:
/// - 0 -> 0, -1 -> 1, 1 -> 2, -2 -> 3, 2 -> 4, ...
pub fn zigzag_decode(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

/// Encode an unsigned integer as varint bytes.
pub fn encode_varint(value: u64) -> Vec<u8> {
    let mut v = value;
    let mut buf = Vec::with_capacity(10);
    while v >= 0x80 {
        buf.push((v as u8 & 0x7F) | 0x80);
        v >>= 7;
    }
    buf.push(v as u8);
    buf
}

/// Zigzag-encode a signed integer to unsigned.
pub fn zigzag_encode(n: i64) -> u64 {
    ((n << 1) ^ (n >> 63)) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_varint_roundtrip() {
        let cases = [0u64, 1, 127, 128, 16383, 16384, 2097151, 2097152];
        for &v in &cases {
            let encoded = encode_varint(v);
            let (decoded, consumed) = decode_varint(&encoded, 0);
            assert_eq!(decoded, v, "roundtrip failed for {v}");
            assert_eq!(consumed, encoded.len(), "consumed mismatch for {v}");
        }
    }

    #[test]
    fn test_zigzag_roundtrip() {
        let cases = [0i64, -1, 1, -2, 2, -100, 100, -10000, 10000];
        for &v in &cases {
            let encoded = zigzag_encode(v);
            let decoded = zigzag_decode(encoded);
            assert_eq!(decoded, v, "zigzag roundtrip failed for {v}");
        }
    }

    #[test]
    fn test_varint_known() {
        assert_eq!(encode_varint(1), vec![0x01]);
        assert_eq!(encode_varint(300), vec![0xAC, 0x02]);
    }
}

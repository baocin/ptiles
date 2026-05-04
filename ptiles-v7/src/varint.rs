/// Varint encoding utilities (protobuf-style, 7 bits per byte, MSB = continuation)
/// plus zigzag encoding for signed integers.

/// Decode a single unsigned varint from bytes starting at `pos`.
/// Returns (value, bytes_consumed).
pub fn decode_varint(data: &[u8], pos: usize) -> (u64, usize) {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    let mut i = pos;
    loop {
        if i >= data.len() {
            break;
        }
        let b = data[i];
        result |= ((b & 0x7F) as u64) << shift;
        i += 1;
        if b & 0x80 == 0 {
            break;
        }
        shift += 7;
    }
    (result, i - pos)
}

/// Encode a u64 as varint bytes, appending to `out`.
pub fn encode_varint(value: u64, out: &mut Vec<u8>) {
    let mut v = value;
    while v >= 0x80 {
        out.push((v as u8 & 0x7F) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

/// Zigzag encode: signed → unsigned (small magnitudes → small values)
/// zigzag(n) = (n << 1) ^ (n >> 63) for i64
pub fn zigzag_encode(n: i64) -> u64 {
    ((n << 1) ^ (n >> 63)) as u64
}

/// Zigzag decode: unsigned → signed
/// zigzag_decode(n) = (n >> 1) ^ -(n & 1)
pub fn zigzag_decode(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

/// Decode a signed integer: decode_varint then zigzag_decode.
pub fn decode_signed_varint(data: &[u8], pos: usize) -> (i64, usize) {
    let (raw, consumed) = decode_varint(data, pos);
    (zigzag_decode(raw), consumed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_varint_roundtrip() {
        let cases = [0u64, 1, 127, 128, 16383, 16384, 2097151, u64::MAX / 2];
        for v in cases {
            let mut buf = Vec::new();
            encode_varint(v, &mut buf);
            let (decoded, consumed) = decode_varint(&buf, 0);
            assert_eq!(decoded, v, "varint roundtrip: {v}");
            assert_eq!(consumed, buf.len());
        }
    }

    #[test]
    fn test_zigzag_roundtrip() {
        for n in -100..=100 {
            let encoded = zigzag_encode(n);
            let decoded = zigzag_decode(encoded);
            assert_eq!(decoded, n, "zigzag roundtrip: {n}");
        }
    }

    #[test]
    fn test_signed_varint() {
        let cases = [0i64, 1, -1, 127, -127, 16383, -16383, 100000, -100000];
        for v in cases {
            let mut buf = Vec::new();
            encode_varint(zigzag_encode(v), &mut buf);
            let (decoded, _) = decode_signed_varint(&buf, 0);
            assert_eq!(decoded, v, "signed varint roundtrip: {v}");
        }
    }
}

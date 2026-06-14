"""
Compression helpers extracted from codec.py.

Pure zstd wrapper functions for dictionary-based compression.
"""

import zstandard as zstd


def train_dictionary(samples: list[bytes], dict_size: int = 512 * 1024) -> bytes:
    """Train a zstd dictionary on sample data."""
    return zstd.train_dictionary(dict_size, samples).as_bytes()


def compress_block(data: bytes, dict_data: bytes, level: int = 12) -> bytes:
    """Compress a data block with zstd dictionary."""
    d = zstd.ZstdCompressionDict(dict_data)
    cctx = zstd.ZstdCompressor(level=level, dict_data=d)
    return cctx.compress(data)


def decompress_block(data: bytes, dict_data: bytes) -> bytes | None:
    """Decompress a data block with zstd dictionary.
    Returns None on failure.
    """
    try:
        d = zstd.ZstdCompressionDict(dict_data)
        dctx = zstd.ZstdDecompressor(dict_data=d)
        return dctx.decompress(data)
    except Exception:
        return None


def decompress_fallback(data: bytes) -> bytes | None:
    """Decompress with no dictionary (fallback for untrained blocks)."""
    try:
        return zstd.ZstdDecompressor().decompress(data)
    except Exception:
        return None

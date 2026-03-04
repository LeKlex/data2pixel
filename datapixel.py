#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
datapixel.py

Encode arbitrary binary data into a black/white PNG and decode it back.

Core idea
---------
- Treat the PNG as a 2D bitfield (one bit per pixel).
- Store bits as grayscale pixels:
    0   -> black
    255 -> white
- Pack the input file into bytes, prepend a small header (magic + original length),
  then write each byte as 8 horizontal pixels.

Image growth
------------
The image is always a square with side length being a power of two:
  32x32, 64x64, 128x128, ...

Capacity (bytes) of an NxN image is:
  (N * N) / 8

Examples:
  32x32  -> 1024 bits  -> 128 bytes
  64x64  -> 4096 bits  -> 512 bytes
  128x128 -> 16384 bits -> 2048 bytes

Because we include a header, the effective maximum payload is (capacity - header_size).

Bit order
---------
This implementation uses LSB-first (least significant bit first) when mapping a byte to pixels:
  bit 0 goes to the leftmost pixel in the 8-pixel group, then bit 1, ... bit 7.

This matches your old script's extraction pattern.

Safety note
-----------
PNG must remain lossless and unmodified. Any operation that changes pixel values
(resize, JPEG conversion, dithering, some "optimize" tools) can corrupt the data.

CLI usage
---------
Encode (requires --out):
  python3 datapixel.py --encode --in=text.txt --out=bin.png

Decode (optional --out):
  python3 datapixel.py --decode --in=bin.png --out=restored.bin

If --out is omitted for --decode, the decoded bytes are written to stdout:
  python3 datapixel.py --decode --in=bin.png
"""

from __future__ import annotations

import argparse
import math
import struct
import sys

import numpy as np
from PIL import Image


# A short signature to identify files produced by this tool.
# This prevents decoding random grayscale images as if they were datapixel images.
MAGIC = b"DPX1"

# Header layout:
#   MAGIC (4 bytes) + original_data_length (unsigned 64-bit little endian, 8 bytes)
# Total header size: 12 bytes
HEADER_FMT = "<4sQ"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def next_pow2(n: int) -> int:
    """
    Return the smallest power of two >= n (for n >= 1).

    Examples:
      next_pow2(1)  -> 1
      next_pow2(2)  -> 2
      next_pow2(3)  -> 4
      next_pow2(32) -> 32
      next_pow2(33) -> 64

    This is used so the PNG size scales as 32, 64, 128, ...
    """
    if n <= 1:
        return 1
    return 1 << (n - 1).bit_length()


def choose_square_size(bits_needed: int, min_size: int = 16) -> int:
    """
    Choose a square image size N (power-of-two) such that N*N >= bits_needed,
    while also ensuring N >= min_size.

    Parameters
    ----------
    bits_needed:
        How many bits we must store in the image. This already includes the header.
    min_size:
        Minimum side length of the square image.

    Returns
    -------
    int
        The chosen side length N.
    """
    if bits_needed <= 0:
        return min_size

    # The smallest integer N such that N*N >= bits_needed
    n = math.isqrt(bits_needed)
    if n * n < bits_needed:
        n += 1

    # Round up to a power of two and enforce minimum size.
    return next_pow2(max(min_size, n))


def pack_payload(data: bytes) -> bytes:
    """
    Prepend a fixed header to the raw input bytes.

    The header allows robust decoding:
    - MAGIC identifies this format (helps detect wrong input).
    - data length tells us exactly how many bytes to extract.

    Returns
    -------
    bytes
        header + data
    """
    header = struct.pack(HEADER_FMT, MAGIC, len(data))
    return header + data


def unpack_payload(payload: bytes) -> bytes:
    """
    Extract the original data bytes from a payload (header + data + optional padding).

    Raises
    ------
    ValueError
        If magic is wrong, payload is too small, or the image doesn't contain
        enough bytes for the declared length (corruption/truncation).
    """
    if len(payload) < HEADER_SIZE:
        raise ValueError("Payload too small to contain header.")

    magic, data_len = struct.unpack(HEADER_FMT, payload[:HEADER_SIZE])
    if magic != MAGIC:
        raise ValueError("Invalid magic header. Not a datapixel image or corrupted data.")

    data = payload[HEADER_SIZE:HEADER_SIZE + data_len]
    if len(data) != data_len:
        raise ValueError("Image does not contain enough data (truncated or corrupted).")

    return data


def _validate_width_multiple_of_8(width: int) -> None:
    """
    Validate that width is divisible by 8.

    Why this constraint exists:
    - We store 1 byte as 8 horizontal pixels.
    - Therefore, image width must support an integer number of bytes per row.
    """
    if width % 8 != 0:
        raise ValueError("Image width must be a multiple of 8 (8 pixels per byte).")


def encode_bytes_to_image_array(payload: bytes, size: int) -> np.ndarray:
    """
    Encode payload bytes into a (size x size) numpy array of dtype uint8.

    Mapping:
      bytes_per_row = size // 8
      byte index i maps to:
        row      = i // bytes_per_row
        col_byte = i %  bytes_per_row
        base_col = col_byte * 8

    Bit order:
      LSB-first (bit 0 is stored first in the 8-pixel group).

    Returns
    -------
    np.ndarray
        (size x size) array containing only 0 or 255.
    """
    _validate_width_multiple_of_8(size)

    # Capacity in bytes: total bits / 8
    bytes_capacity = (size * size) // 8
    if len(payload) > bytes_capacity:
        raise ValueError(
            f"Payload ({len(payload)} bytes) does not fit into {size}x{size} image "
            f"(capacity {bytes_capacity} bytes)."
        )

    img = np.zeros((size, size), dtype=np.uint8)
    bytes_per_row = size // 8

    for byte_idx, b in enumerate(payload):
        row = byte_idx // bytes_per_row
        col_byte = byte_idx % bytes_per_row
        base_col = col_byte * 8

        # Complex part: bit extraction and mapping.
        # LSB-first is intentional to match the legacy script behavior.
        for bit in range(8):
            bit_val = (b >> bit) & 1
            img[row, base_col + bit] = 255 if bit_val else 0

    return img


def decode_image_array_to_bytes(img: np.ndarray) -> bytes:
    """
    Decode an image array back into raw bytes using the same mapping as the encoder.

    Pixels are thresholded to bits:
      pixel > 127 => bit 1
      else        => bit 0

    Returns
    -------
    bytes
        Decoded bytes. This includes header + original data + any padding bytes.
    """
    if img.ndim != 2:
        raise ValueError("Expected a grayscale image array (2D).")

    height, width = img.shape
    _validate_width_multiple_of_8(width)

    # Total pixel count must be divisible by 8 so it represents a whole number of bytes.
    if (width * height) % 8 != 0:
        raise ValueError("Image total pixel count must be divisible by 8 to decode.")

    bytes_per_row = width // 8
    bytes_total = (width * height) // 8

    bits = (img > 127).astype(np.uint8)

    out = bytearray(bytes_total)

    for byte_idx in range(bytes_total):
        row = byte_idx // bytes_per_row
        col_byte = byte_idx % bytes_per_row
        base_col = col_byte * 8

        val = 0
        for bit in range(8):
            val |= int(bits[row, base_col + bit]) << bit
        out[byte_idx] = val

    return bytes(out)


def cmd_encode(in_path: str, out_path: str) -> None:
    """
    Encode a file into a PNG.

    Steps:
    1) Read input bytes
    2) Prepend header (magic + length)
    3) Choose an image size that fits payload bits
    4) Encode into 0/255 pixel array
    5) Save as lossless PNG
    """
    with open(in_path, "rb") as f:
        data = f.read()

    payload = pack_payload(data)
    bits_needed = len(payload) * 8

    size = choose_square_size(bits_needed, min_size=16)
    img_arr = encode_bytes_to_image_array(payload, size=size)

    image = Image.fromarray(img_arr, mode="L")
    image.save(out_path, format="PNG")

    capacity_bytes = (size * size) // 8
    sys.stderr.write(
        f"Encoded {len(data)} bytes into {size}x{size} PNG. "
        f"Capacity {capacity_bytes} bytes; payload {len(payload)} bytes incl. {HEADER_SIZE}-byte header.\n"
    )


def cmd_decode(in_path: str, out_path: str | None) -> None:
    """
    Decode a PNG back into the original bytes.

    If out_path is None, decoded bytes are written to stdout (binary-safe).
    Informational messages are written to stderr to keep stdout clean.
    """
    image = Image.open(in_path).convert("L")
    img_arr = np.array(image, dtype=np.uint8)

    payload = decode_image_array_to_bytes(img_arr)
    data = unpack_payload(payload)

    if out_path:
        with open(out_path, "wb") as f:
            f.write(data)
        sys.stderr.write(f"Decoded {len(data)} bytes to '{out_path}'.\n")
    else:
        # Write raw bytes to stdout; this works for text and binary.
        # If you only want text, you can pipe it through a decoder (e.g., iconv) or open with proper encoding.
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        sys.stderr.write(f"Decoded {len(data)} bytes to stdout.\n")


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Create and return the CLI argument parser.

    Notes:
    - --out is required for --encode.
    - --out is optional for --decode; if omitted, output is written to stdout.
    """
    p = argparse.ArgumentParser(
        prog="datapixel.py",
        description="Encode/decode binary data into/from a black/white PNG.",
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--encode", action="store_true", help="Encode file -> PNG")
    mode.add_argument("--decode", action="store_true", help="Decode PNG -> file or stdout")

    p.add_argument("--in", dest="in_path", required=True, help="Input file path")
    p.add_argument("--out", dest="out_path", required=False, help="Output file path (optional for --decode)")
    return p


def main() -> int:
    """
    Main entry point.

    Returns
    -------
    int
        Process exit code.
    """
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.encode:
        if not args.out_path:
            parser.error("--out is required when using --encode.")
        cmd_encode(args.in_path, args.out_path)
        return 0

    if args.decode:
        cmd_decode(args.in_path, args.out_path)
        return 0

    # Unreachable due to argparse mutual exclusivity.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
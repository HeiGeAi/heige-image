"""Validation and atomic persistence for generated image bytes."""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from pathlib import Path


MAX_IMAGE_BYTES = 20 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ALLOWED_CONTENT_TYPES = {"image/png"}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
_MAX_PIXELS = 32_000_000
_MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024


class ImageResponseError(ValueError):
    """The remote response is not a supported, bounded image."""


class OutputPathError(ValueError):
    """The requested output path is unsafe."""


def _validate_png(data: bytes) -> None:
    if not data.startswith(_PNG_MAGIC):
        raise ImageResponseError("响应内容不是有效的 PNG 图片")

    offset = len(_PNG_MAGIC)
    first = True
    width = height = bit_depth = color_type = None
    idat_parts = []
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ImageResponseError("PNG chunk 被截断")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ImageResponseError("PNG chunk 数据被截断")
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        if zlib.crc32(kind + payload) != expected_crc:
            raise ImageResponseError("PNG chunk CRC 校验失败")
        if first and kind != b"IHDR":
            raise ImageResponseError("PNG 第一个 chunk 必须是 IHDR")
        first = False

        if kind == b"IHDR":
            if length != 13 or width is not None:
                raise ImageResponseError("PNG IHDR 无效")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if not width or not height or width * height > _MAX_PIXELS:
                raise ImageResponseError("PNG 尺寸无效或过大")
            if color_type not in _PNG_BIT_DEPTHS or bit_depth not in _PNG_BIT_DEPTHS[color_type]:
                raise ImageResponseError("PNG 颜色类型或位深无效")
            if compression != 0 or filtering != 0 or interlace != 0:
                raise ImageResponseError("PNG 仅支持标准非隔行编码")
        elif kind == b"IDAT":
            if width is None:
                raise ImageResponseError("PNG IDAT 位于 IHDR 之前")
            idat_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or not idat_parts:
                raise ImageResponseError("PNG IEND 无效或缺少 IDAT")
            if end != len(data):
                raise ImageResponseError("PNG IEND 后存在多余数据")
            saw_iend = True
            break
        elif kind[:1].isupper() and kind not in {b"PLTE"}:
            raise ImageResponseError(f"PNG 包含未知关键 chunk: {kind!r}")
        offset = end

    if not saw_iend:
        raise ImageResponseError("PNG 缺少 IEND")

    expected_size = ((width * _PNG_CHANNELS[color_type] * bit_depth + 7) // 8 + 1) * height
    if expected_size > _MAX_DECOMPRESSED_BYTES:
        raise ImageResponseError("PNG 解压后数据过大")
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(b"".join(idat_parts), expected_size + 1)
    except zlib.error as exc:
        raise ImageResponseError(f"PNG IDAT 压缩流无效: {exc}") from exc
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ImageResponseError("PNG IDAT 压缩流被截断或包含多余数据")
    if len(decoded) != expected_size:
        raise ImageResponseError("PNG 像素数据长度与 IHDR 不一致")


def validate_image_bytes(
    data: bytes,
    *,
    content_type: str | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> bytes:
    """Return a complete PNG payload or raise a user-facing validation error."""
    if not data:
        raise ImageResponseError("图片响应为空")
    if len(data) > max_bytes:
        raise ImageResponseError(f"图片响应过大，最大允许 {max_bytes} 字节")

    normalized_type = None
    if content_type is not None:
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_type not in _ALLOWED_CONTENT_TYPES:
            raise ImageResponseError(f"不支持的图片 Content-Type: {content_type}")

    _validate_png(data)
    return data


def validate_image_response(response, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """Read and validate a streamed HTTP PNG response with a hard byte limit."""
    content_type = response.headers.get("Content-Type")
    if not content_type:
        raise ImageResponseError("图片响应缺少 Content-Type")
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in _ALLOWED_CONTENT_TYPES:
        raise ImageResponseError(f"不支持的图片 Content-Type: {content_type}")
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            parsed_length = int(content_length)
        except ValueError as exc:
            raise ImageResponseError("图片响应的 Content-Length 无效") from exc
        if parsed_length < 0:
            raise ImageResponseError("图片响应的 Content-Length 无效")
        if parsed_length > max_bytes:
            raise ImageResponseError(f"图片响应过大，最大允许 {max_bytes} 字节")
    payload = bytearray()
    for chunk in response.iter_bytes():
        if len(payload) + len(chunk) > max_bytes:
            raise ImageResponseError(f"图片响应过大，最大允许 {max_bytes} 字节")
        payload.extend(chunk)
    return validate_image_bytes(
        bytes(payload),
        content_type=content_type,
        max_bytes=max_bytes,
    )


def validate_output_path(output_path: str | Path) -> Path:
    """Validate and prepare a PNG destination before any paid request."""
    requested = Path(output_path).expanduser()
    if requested.suffix.lower() != ".png":
        raise OutputPathError(f"输出文件必须使用 .png 扩展名: {requested}")
    if requested.is_symlink():
        raise OutputPathError(f"输出路径不能是符号链接: {requested}")

    # 父目录允许是符号链接（如 macOS 的 /tmp -> /private/tmp）：解析成真实路径后落盘即可，
    # 真正要拒的只有最终落点文件本身是 symlink（下方 output.is_symlink() 检查）。
    absolute_parent = Path(os.path.abspath(requested.parent))
    absolute_parent.mkdir(parents=True, exist_ok=True)
    real_parent = absolute_parent.resolve(strict=True)
    if not real_parent.is_dir():
        raise OutputPathError(f"输出父目录不是目录: {real_parent}")

    output = real_parent / requested.name
    if output.is_symlink():
        raise OutputPathError(f"输出路径不能是符号链接: {output}")
    if output.exists() and not output.is_file():
        raise OutputPathError(f"输出路径不是普通文件: {output}")
    if not os.access(real_parent, os.W_OK | os.X_OK):
        raise OutputPathError(f"输出父目录不可写: {real_parent}")
    return output


def atomic_write_image(output_path: str | Path, data: bytes) -> Path:
    """Atomically write validated image data without following an output symlink."""
    validate_image_bytes(data)
    output = validate_output_path(output_path)
    real_parent = output.parent

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".heige-image-",
            dir=real_parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        if output.is_symlink():
            raise OutputPathError(f"输出路径不能是符号链接: {output}")
        os.replace(temp_path, output)
        temp_path = None
        return output
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

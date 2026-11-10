"""二维码生成：落盘 ``server/qr.png`` + 内存 PNG 供 ``/api/qr.png`` 返回。

主路径用 ``qrcode[pil]``（requirements.txt / start.bat 会自动安装）。若该可选
依赖缺失（例如刚 clone 还没跑 start.bat），自动降级到本模块自带的**纯 Python
QR 编码器**（byte mode，version 1..5 / ECC L，单块，8 种 mask 自动择优），
仍然能生成真正可扫的二维码，服务启动与扫码流程都不会中断。

二维码内容固定为 ``http://<LAN_IP>:<PORT>/``（PROTOCOL 第 7 节）。
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw
    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - pillow 缺失时由 start.bat 安装
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

try:
    import qrcode  # type: ignore[import-not-found]
    from qrcode.constants import ERROR_CORRECT_M  # type: ignore[import-not-found]
    _QRCODE_AVAILABLE = True
except Exception:
    qrcode = None  # type: ignore[assignment]
    ERROR_CORRECT_M = None
    _QRCODE_AVAILABLE = False

_SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_QR_PATH = _SERVER_DIR / "qr.png"
MODULE_PIXELS = 8
QUIET_ZONE_MODULES = 4


class QRError(RuntimeError):
    """二维码生成失败（内容超长或图像库不可用）。"""


def make_url(lan_ip: str, port: int, https: bool = False) -> str:
    """拼二维码内容。

    ``https=True`` 时给 ``https://`` 前缀：手机浏览器只在安全上下文暴露
    ``navigator.mediaDevices``，语音输入必须走这个入口（见 ``certs.py``）。
    """
    scheme = "https" if https else "http"
    return f"{scheme}://{lan_ip}:{int(port)}/"


def backend_name() -> str:
    """当前实际使用的后端，写进 README 的自测结论。"""
    return "qrcode[pil]" if _QRCODE_AVAILABLE else "pure-python-fallback"


def qrcode_available() -> bool:
    return _QRCODE_AVAILABLE


# ===========================================================================
# 纯 Python QR 编码器（降级用）
# ===========================================================================
# version → (data codewords, ecc codewords)，ECC level = L，version 1..5 均为单块
_L_CAPACITY: dict[int, tuple[int, int]] = {
    1: (19, 7),
    2: (34, 10),
    3: (55, 15),
    4: (80, 20),
    5: (108, 26),
}


def _gf_mul(x: int, y: int) -> int:
    """GF(2^8) 乘法，本原多项式 0x11D。"""
    result = 0
    for _ in range(8):
        if y & 1:
            result ^= x
        high = x & 0x80
        x = (x << 1) & 0xFF
        if high:
            x ^= 0x1D
        y >>= 1
    return result & 0xFF


def _rs_divisor(degree: int) -> list[int]:
    """生成 Reed-Solomon 生成多项式系数（Nayuki 风格，高次在前）。"""
    result = [0] * degree
    result[degree - 1] = 1
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result[0]
        result = result[1:] + [0]
        for i in range(len(result)):
            result[i] ^= _gf_mul(divisor[i], factor)
    return result


def _append_bits(bits: list[int], value: int, length: int) -> None:
    for shift in range(length - 1, -1, -1):
        bits.append((value >> shift) & 1)


def _codewords(payload: bytes) -> tuple[int, list[int]]:
    """byte mode 编码 + RS 纠错，返回 ``(version, 全部码字)``。"""
    needed_bits = 4 + 8 + 8 * len(payload)
    version = 0
    for candidate in sorted(_L_CAPACITY):
        data_capacity, _ecc = _L_CAPACITY[candidate]
        if needed_bits <= data_capacity * 8:
            version = candidate
            break
    if version == 0:
        raise QRError(
            f"二维码内容过长（{len(payload)} 字节），纯 Python 降级编码器最多支持 92 字节"
        )
    data_capacity, ecc_count = _L_CAPACITY[version]
    capacity_bits = data_capacity * 8

    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)          # byte mode
    _append_bits(bits, len(payload), 8)    # 字符计数（version 1..9 为 8 位）
    for byte in payload:
        _append_bits(bits, byte, 8)
    terminator = min(4, capacity_bits - len(bits))
    _append_bits(bits, 0, terminator)
    while len(bits) % 8 != 0:
        bits.append(0)
    pad = 0xEC
    while len(bits) < capacity_bits:
        _append_bits(bits, pad, 8)
        pad = 0x11 if pad == 0xEC else 0xEC

    data_words: list[int] = []
    for i in range(0, len(bits), 8):
        word = 0
        for bit in bits[i : i + 8]:
            word = (word << 1) | bit
        data_words.append(word)
    ecc_words = _rs_remainder(data_words, _rs_divisor(ecc_count))
    return version, data_words + ecc_words


def _blank_matrix(version: int) -> tuple[list[list[int]], list[list[bool]]]:
    size = version * 4 + 17
    matrix = [[0 for _ in range(size)] for _ in range(size)]
    reserved = [[False for _ in range(size)] for _ in range(size)]

    def put(row: int, col: int, dark: bool) -> None:
        if 0 <= row < size and 0 <= col < size:
            matrix[row][col] = 1 if dark else 0
            reserved[row][col] = True

    # 定位图形（时序）先画，随后由 finder 覆盖
    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)

    def finder(center_x: int, center_y: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                dist = max(abs(dx), abs(dy))
                put(center_y + dy, center_x + dx, dist <= 3 and dist != 2)

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)

    # 校正图形：version 2..5 只有右下角一个，中心 (size-7, size-7)
    if version >= 2:
        cx = size - 7
        cy = size - 7
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(cy + dy, cx + dx, max(abs(dx), abs(dy)) != 1)

    # 预留格式信息区（含 dark module）
    for i in range(9):
        reserved[i][8] = True
        reserved[8][i] = True
    for i in range(size - 8, size):
        reserved[i][8] = True
        reserved[8][i] = True
    return matrix, reserved


def _format_bits(mask: int) -> int:
    # ECC level L 的格式位为 0b01
    data = (0b01 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (0x537 if ((rem >> 9) & 1) else 0)
    return ((data << 10) | rem) ^ 0x5412


def _draw_format(matrix: list[list[int]], size: int, mask: int) -> None:
    bits = _format_bits(mask)

    def bit(index: int) -> bool:
        return ((bits >> index) & 1) == 1

    for i in range(6):
        matrix[i][8] = 1 if bit(i) else 0
    matrix[7][8] = 1 if bit(6) else 0
    matrix[8][8] = 1 if bit(7) else 0
    matrix[8][7] = 1 if bit(8) else 0
    for i in range(9, 15):
        matrix[8][14 - i] = 1 if bit(i) else 0
    for i in range(8):
        matrix[8][size - 1 - i] = 1 if bit(i) else 0
    for i in range(8, 15):
        matrix[size - 15 + i][8] = 1 if bit(i) else 0
    matrix[size - 8][8] = 1  # dark module 恒为深色


def _mask_bit(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _penalty(matrix: list[list[int]]) -> int:
    size = len(matrix)
    score = 0

    # N1：连续同色 >= 5
    for line in list(matrix) + [
        [matrix[y][x] for y in range(size)] for x in range(size)
    ]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # N2：2x2 同色块
    for y in range(size - 1):
        for x in range(size - 1):
            color = matrix[y][x]
            if (
                matrix[y][x + 1] == color
                and matrix[y + 1][x] == color
                and matrix[y + 1][x + 1] == color
            ):
                score += 3

    # N3：1:1:3:1:1 类似定位图形的 11 模块序列
    pattern_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pattern_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for y in range(size):
        for x in range(size - 10):
            window = matrix[y][x : x + 11]
            if window == pattern_a or window == pattern_b:
                score += 40
    for x in range(size):
        for y in range(size - 10):
            window = [matrix[y + k][x] for k in range(11)]
            if window == pattern_a or window == pattern_b:
                score += 40

    # N4：深色比例偏离 50%
    dark = sum(sum(row) for row in matrix)
    deviation = int(abs(dark * 100.0 / (size * size) - 50.0) // 5)
    score += deviation * 10
    return score


def _encode_matrix(payload: bytes) -> list[list[int]]:
    version, words = _codewords(payload)
    base, reserved = _blank_matrix(version)
    size = len(base)

    data_bits: list[int] = []
    for word in words:
        _append_bits(data_bits, word, 8)

    index = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            for j in range(2):
                col = right - j
                row = (size - 1 - vert) if upward else vert
                if not reserved[row][col] and index < len(data_bits):
                    base[row][col] = data_bits[index]
                    index += 1
        right -= 2

    best_matrix: list[list[int]] | None = None
    best_score: int | None = None
    for mask in range(8):
        candidate = [row[:] for row in base]
        for row in range(size):
            for col in range(size):
                if not reserved[row][col] and _mask_bit(mask, row, col):
                    candidate[row][col] ^= 1
        _draw_format(candidate, size, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best_score = score
            best_matrix = candidate
    if best_matrix is None:  # pragma: no cover - 8 次循环必然命中
        raise QRError("未能选出二维码 mask")
    return best_matrix


def _render_png(matrix: list[list[int]]) -> bytes:
    if not _PIL_AVAILABLE:
        raise QRError("Pillow 不可用，请先运行 start.bat 安装依赖")
    size = len(matrix)
    modules = size + QUIET_ZONE_MODULES * 2
    side = modules * MODULE_PIXELS
    image = Image.new("RGB", (side, side), "white")
    draw = ImageDraw.Draw(image)
    for row in range(size):
        for col in range(size):
            if matrix[row][col]:
                x0 = (col + QUIET_ZONE_MODULES) * MODULE_PIXELS
                y0 = (row + QUIET_ZONE_MODULES) * MODULE_PIXELS
                draw.rectangle(
                    [x0, y0, x0 + MODULE_PIXELS - 1, y0 + MODULE_PIXELS - 1],
                    fill="black",
                )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_png_via_qrcode(url: str) -> bytes:
    code = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=MODULE_PIXELS,
        border=QUIET_ZONE_MODULES,
    )
    code.add_data(url)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white").convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ===========================================================================
# 对外接口
# ===========================================================================
def build_png(url: str) -> bytes:
    """返回编码 ``url`` 的二维码 PNG 字节。"""
    if not isinstance(url, str) or not url:
        raise QRError("二维码内容不能为空")
    if _QRCODE_AVAILABLE:
        try:
            return _build_png_via_qrcode(url)
        except Exception as exc:  # 主路径异常时也降级，不让服务挂掉
            print(f"[qr] qrcode 后端失败，改用纯 Python 编码器：{exc}")
    return _render_png(_encode_matrix(url.encode("utf-8")))


def write_qr(url: str, path: str | Path | None = None) -> bytes:
    """生成 PNG、原子落盘到 ``qr.png``，并返回同样的 PNG 字节。"""
    png = build_png(url)
    target = Path(path) if path else DEFAULT_QR_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(png)
    os_replace(tmp, target)
    return png


def os_replace(source: Path, target: Path) -> None:
    """单独封装，便于静态自检核对原子写语义。"""
    import os

    os.replace(source, target)


def qr_dimensions(url: str) -> dict[str, Any]:
    """返回二维码元信息，供 ``/api/status`` 之外的调试/README 使用。"""
    if _QRCODE_AVAILABLE:
        code = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=MODULE_PIXELS,
            border=QUIET_ZONE_MODULES,
        )
        code.add_data(url)
        code.make(fit=True)
        modules = code.modules_count
        return {
            "backend": "qrcode[pil]",
            "version": code.version,
            "modules": modules,
            "pixels": (modules + QUIET_ZONE_MODULES * 2) * MODULE_PIXELS,
        }
    version, _words = _codewords(url.encode("utf-8"))
    modules = version * 4 + 17
    return {
        "backend": "pure-python-fallback",
        "version": version,
        "modules": modules,
        "pixels": (modules + QUIET_ZONE_MODULES * 2) * MODULE_PIXELS,
    }

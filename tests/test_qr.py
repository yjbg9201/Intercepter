"""QR コードの生成（ui/qr.py）。依存を増やさないよう、テスト内の小さな復号器で読み戻して確かめる。

2026-11-17 に、参照実装（segno）とのセル単位の比較（詰め物のない全長データ・全マスク・型番 1〜5 で一致）と、
OpenCV の読み取り器での復号（URL の形の入力 200 件で誤読 0 件）を、開発用の別環境で確認してある。
"""

import pytest

from takeover.ui import qr


def decode(matrix):
    """qr_matrix の出力を読み戻す（形式情報 → マスク解除 → コード語 → 誤り訂正の検算 → バイト列）。"""
    size = len(matrix)
    version = (size - 17) // 4
    bits = [matrix[i][8] for i in range(6)] + [matrix[7][8], matrix[8][8], matrix[8][7]]
    bits += [matrix[8][14 - i] for i in range(9, 15)]
    fmt = sum(int(b) << i for i, b in enumerate(bits)) ^ 0x5412
    second = [matrix[8][size - 1 - i] for i in range(8)] + [matrix[size - 15 + i][8] for i in range(8, 15)]
    assert sum(int(b) << i for i, b in enumerate(second)) ^ 0x5412 == fmt  # 2 か所の形式情報が同じ
    level, mask = fmt >> 13, (fmt >> 10) & 7
    assert level == 1 and qr._format_bits(mask) ^ 0x5412 == fmt  # レベル L、BCH も正しい
    function = [[False] * size for _ in range(size)]
    for i in range(size):
        function[6][i] = function[i][6] = True
    for fr, fc in ((3, 3), (3, size - 4), (size - 4, 3)):
        for dr in range(-4, 5):
            for dc in range(-4, 5):
                if 0 <= fr + dr < size and 0 <= fc + dc < size:
                    function[fr + dr][fc + dc] = True
    if version >= 2:
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                function[size - 7 + dr][size - 7 + dc] = True
    for i in range(9):
        function[8][i] = function[i][8] = True
    for i in range(8):
        function[8][size - 1 - i] = function[size - 1 - i][8] = True
    stream = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            r = size - 1 - vert if upward else vert
            for c in (right, right - 1):
                if not function[r][c]:
                    stream.append(int(matrix[r][c] ^ qr._MASKS[mask](r, c)))
        right -= 2
    capacity, ec = qr._CAPACITY[version]
    words = [int("".join(map(str, stream[i:i + 8])), 2) for i in range(0, (capacity + ec) * 8, 8)]
    assert qr.rs_remainder(words[:capacity], ec) == words[capacity:]
    data_bits = "".join(f"{w:08b}" for w in words[:capacity])
    assert data_bits[:4] == "0100"  # バイトモード
    count = int(data_bits[4:12], 2)
    return bytes(int(data_bits[12 + 8 * i:20 + 8 * i], 2) for i in range(count))


@pytest.mark.parametrize("text, size", [
    ("a", 21), ("x" * 17, 21), ("x" * 18, 25), ("y" * 32, 25), ("z" * 53, 29), ("w" * 78, 33), ("q" * 106, 37),
    ("http://192.168.1.23:8765/?token=AbCdEfGhIjKlMnOpQrStUv", 33),
    ("日本語も UTF-8 のバイト列として入る", 29),
])
def test_roundtrip_and_size(text, size):
    matrix = qr.qr_matrix(text)
    assert len(matrix) == size and all(len(row) == size for row in matrix)
    assert decode(matrix) == text.encode("utf-8")


def test_fixed_patterns():
    matrix = qr.qr_matrix("hello")
    size = len(matrix)
    finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert ["".join("1" if matrix[r0 + r][c0 + c] else "0" for c in range(7)) for r in range(7)] == finder
    assert all(matrix[6][c] == (c % 2 == 0) for c in range(8, size - 8))  # タイミングパターン
    assert matrix[size - 8][8] is True  # 常に暗のモジュール
    assert all(not matrix[7][c] for c in range(8))  # 分離（明）


def test_reed_solomon_known_value():
    # 規格の付属書にある例（型番 1-M の "01234567"）のデータコード語と誤り訂正コード語
    data = [0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11]
    assert qr.rs_remainder(data, 10) == [0xA5, 0x24, 0xD4, 0xC1, 0xED, 0x36, 0xC7, 0x87, 0x2C, 0x55]


def test_too_long_returns_none_and_rows_format():
    assert qr.qr_matrix("x" * 107) is None and qr.qr_rows("x" * 107) is None
    rows = qr.qr_rows("abc")
    assert len(rows) == 21 and all(len(row) == 21 and set(row) <= {"0", "1"} for row in rows)

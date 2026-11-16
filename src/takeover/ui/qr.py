"""QR コードの生成（純関数。依存なし）。スマホで開く URL を PC の画面に出すために使う（SPEC §6.7）。

対応範囲を必要な分だけに絞ってある:
- バイトモード、誤り訂正レベル L、型番 1〜5（RS ブロックが 1 つで済む範囲。最大 106 バイト）。
  トークン付きの URL（60 バイト前後）は型番 4 に収まる。収まらないデータには None を返す。
- マスクは 8 種類を試し、規格の評価点が最も低いものを選ぶ。

返り値は「行のリスト」で、各行は True（暗）/ False（明）のリスト。周囲の余白（クワイエットゾーン）は含めない。
"""

from __future__ import annotations

# 型番 → (データコード語数, 誤り訂正コード語数)。レベル L、ブロック 1 つ
_CAPACITY = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}
_FORMAT_LEVEL_L = 1


def _gf_tables() -> tuple[list[int], list[int]]:
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def rs_divisor(degree: int) -> list[int]:
    """リード・ソロモン符号の生成多項式 (x - α^0)(x - α^1)…(x - α^(degree-1)) の係数（最高次の 1 を除き、高次から）。"""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def rs_remainder(data: list[int], degree: int) -> list[int]:
    """data に付ける誤り訂正コード語（degree 個）。"""
    divisor = rs_divisor(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(divisor):
            result[index] ^= _gf_mul(coefficient, factor)
    return result


def _codewords(data: bytes, version: int) -> list[int]:
    capacity, ec = _CAPACITY[version]
    bits: list[int] = [0, 1, 0, 0]  # バイトモード
    bits += [(len(data) >> i) & 1 for i in range(7, -1, -1)]  # 文字数（型番 1〜9 は 8 ビット）
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))  # 終端
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    words += [pad[i % 2] for i in range(capacity - len(words))]
    return words + rs_remainder(words, ec)


def _format_bits(mask: int) -> int:
    data = (_FORMAT_LEVEL_L << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _penalty(m: list[list[bool]]) -> int:
    size = len(m)
    total = 0
    lines = [row for row in m] + [[m[r][c] for r in range(size)] for c in range(size)]
    # 位置検出パターンに似た並び（暗 1:明 1:暗 3:明 1:暗 1 の前か後ろに明が 4 個）
    patterns = ([True, False, True, True, True, False, True, False, False, False, False],
                [False, False, False, False, True, False, True, True, True, False, True])
    for line in lines:
        run = 1
        for i in range(1, size):  # 同色が 5 個以上続く
            if line[i] == line[i - 1]:
                run += 1
            else:
                total += run - 2 if run >= 5 else 0
                run = 1
        total += run - 2 if run >= 5 else 0
        for i in range(size - 10):
            if line[i:i + 11] in patterns:
                total += 40
    for r in range(size - 1):  # 2x2 の同色ブロック
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                total += 3
    dark = sum(sum(row) for row in m)
    total += 10 * (abs(dark * 20 - size * size * 10) // (size * size))  # 暗の割合が 50% から離れるほど
    return total


def qr_matrix(data: bytes | str) -> list[list[bool]] | None:
    """data を QR コードにする。型番 1〜5（レベル L）に収まらなければ None。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    version = next((v for v, (capacity, _) in _CAPACITY.items() if len(data) <= capacity - 2), None)
    if version is None:
        return None
    size = 17 + 4 * version
    base = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def put(r: int, c: int, dark: bool) -> None:
        base[r][c] = dark
        function[r][c] = True

    for i in range(size):  # タイミングパターン
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for fr, fc in ((3, 3), (3, size - 4), (size - 4, 3)):  # 位置検出パターン＋分離
        for dr in range(-4, 5):
            for dc in range(-4, 5):
                r, c = fr + dr, fc + dc
                if 0 <= r < size and 0 <= c < size:
                    dist = max(abs(dr), abs(dc))
                    put(r, c, dist not in (2, 4))
    if version >= 2:  # 位置合わせパターン（型番 2〜6 は 1 つ）
        center = size - 7
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                put(center + dr, center + dc, max(abs(dr), abs(dc)) != 1)
    for i in range(9):  # 形式情報の場所を予約（値は後で書く）
        for r, c in ((8, i), (i, 8)):
            if not function[r][c]:
                put(r, c, False)
    for i in range(8):
        put(8, size - 1 - i, False)
        put(size - 1 - i, 8, False)
    put(size - 8, 8, True)  # 常に暗のモジュール

    words = _codewords(data, version)
    bits = [(w >> i) & 1 for w in words for i in range(7, -1, -1)]
    index = 0
    right = size - 1
    while right >= 1:  # 右下から 2 列ずつ、上下に折り返しながら置く
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            r = size - 1 - vert if upward else vert
            for c in (right, right - 1):
                if not function[r][c]:
                    base[r][c] = bool(bits[index]) if index < len(bits) else False
                    index += 1
        right -= 2

    best: tuple[int, list[list[bool]]] | None = None
    for mask, rule in enumerate(_MASKS):
        m = [[base[r][c] ^ (not function[r][c] and rule(r, c)) for c in range(size)] for r in range(size)]
        fmt = _format_bits(mask)
        bit = lambda i: bool((fmt >> i) & 1)  # noqa: E731
        for i in range(6):
            m[i][8] = bit(i)
        m[7][8], m[8][8], m[8][7] = bit(6), bit(7), bit(8)
        for i in range(9, 15):
            m[8][14 - i] = bit(i)
        for i in range(8):
            m[8][size - 1 - i] = bit(i)
        for i in range(8, 15):
            m[size - 15 + i][8] = bit(i)
        m[size - 8][8] = True
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1] if best else None


def qr_rows(data: bytes | str) -> list[str] | None:
    """UI へ渡す形（各行を "0" / "1" の文字列にしたもの）。収まらなければ None。"""
    matrix = qr_matrix(data)
    if matrix is None:
        return None
    return ["".join("1" if cell else "0" for cell in row) for row in matrix]

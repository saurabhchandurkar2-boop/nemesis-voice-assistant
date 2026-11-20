"""
Tiny offline QR code generator for Nemesis (byte mode, error correction L, versions 1-10).
No internet and no extra install needed.  qr_svg(text) returns an SVG string.
"""
_ECC = [7, 10, 15, 20, 26, 18, 20, 24, 30, 18]           # error-correction codewords per block, level L
_BLOCKS = [1, 1, 1, 1, 1, 2, 2, 2, 2, 4]                 # number of blocks, level L


def _raw_codewords(v):
    r = (16 * v + 128) * v + 64
    if v >= 2:
        n = v // 7 + 2
        r -= (25 * n - 10) * n - 55
        if v >= 7:
            r -= 36
    return r // 8


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(deg):
    res = [0] * (deg - 1) + [1]
    root = 1
    for _ in range(deg):
        for j in range(deg):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < deg:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data, divisor):
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(divisor):
            res[i] ^= _gf_mul(c, factor)
    return res


def _bit(x, i):
    return (x >> i) & 1


def _build(text):
    data_bytes = text.encode("utf-8")
    ver = None
    for v in range(1, 11):
        data_cw = _raw_codewords(v) - _ECC[v - 1] * _BLOCKS[v - 1]
        need_bits = 4 + (16 if v >= 10 else 8) + 8 * len(data_bytes)
        if need_bits <= data_cw * 8:
            ver = v
            break
    if ver is None:
        raise ValueError("text too long for the built-in QR generator")
    v = ver
    data_cw = _raw_codewords(v) - _ECC[v - 1] * _BLOCKS[v - 1]
    bits = [0, 1, 0, 0]
    cnt_len = 16 if v >= 10 else 8
    bits += [(len(data_bytes) >> i) & 1 for i in range(cnt_len - 1, -1, -1)]
    for b in data_bytes:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, data_cw * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(cw) < data_cw:
        cw.append(pad)
        pad ^= 0xEC ^ 0x11

    # blocks + error correction, interleaved
    nb, ecc, total = _BLOCKS[v - 1], _ECC[v - 1], _raw_codewords(v)
    short_len, n_short = total // nb, nb - total % nb
    div = _rs_divisor(ecc)
    blocks, k = [], 0
    for i in range(nb):
        dat = cw[k:k + short_len - ecc + (0 if i < n_short else 1)]
        k += len(dat)
        e = _rs_remainder(dat, div)
        if i < n_short:
            dat = dat + [0]
        blocks.append(dat + e)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc or j >= n_short:
                final.append(blk[i])

    size = 17 + 4 * v
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            mod[y][x] = bool(dark)
            fn[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)

    if v >= 2:
        n = v // 7 + 2
        step = ((v * 4 + 4) // (n * 2 - 2) + (1 if (v * 4 + 4) % (n * 2 - 2) else 0)) * 2
        pos = [6]
        p = size - 7
        for _ in range(n - 1):
            pos.insert(1, p)
            p -= step
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        setf(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask):
        d = (1 << 3) | mask                      # error level L = 01
        r = d
        for _ in range(10):
            r = (r << 1) ^ ((r >> 9) * 0x537)
        b = ((d << 10) | r) ^ 0x5412
        for i in range(6):
            setf(8, i, _bit(b, i))
        setf(8, 7, _bit(b, 6))
        setf(8, 8, _bit(b, 7))
        setf(7, 8, _bit(b, 8))
        for i in range(9, 15):
            setf(14 - i, 8, _bit(b, i))
        for i in range(8):
            setf(size - 1 - i, 8, _bit(b, i))
        for i in range(8, 15):
            setf(8, size - 15 + i, _bit(b, i))
        setf(8, size - 8, True)

    format_bits(0)                               # reserve the area
    if v >= 7:
        r = v
        for _ in range(12):
            r = (r << 1) ^ ((r >> 11) * 0x1F25)
        b = (v << 12) | r
        for i in range(18):
            a, c = size - 11 + i % 3, i // 3
            setf(a, c, _bit(b, i))
            setf(c, a, _bit(b, i))

    # place data
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = size - 1 - vert if up else vert
                if not fn[y][x] and i < len(final) * 8:
                    mod[y][x] = bool(_bit(final[i >> 3], 7 - (i & 7)))
                    i += 1
        right -= 2

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def penalty(m):
        score = 0
        for grid in (m, [list(r) for r in zip(*m)]):
            for row in grid:
                run, prev = 0, None
                for c in row:
                    if c == prev:
                        run += 1
                        if run == 5:
                            score += 3
                        elif run > 5:
                            score += 1
                    else:
                        prev, run = c, 1
                s = "".join("1" if c else "0" for c in row)
                for pat in ("00001011101", "10111010000"):
                    start = 0
                    while True:
                        k = s.find(pat, start)
                        if k < 0:
                            break
                        score += 40
                        start = k + 1
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(c for row in m for c in row)
        k = abs(dark * 20 - size * size * 10) // (size * size)
        return score + k * 10

    best, best_m = None, 0
    for mk in range(8):
        trial = [row[:] for row in mod]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and masks[mk](x, y):
                    trial[y][x] = not trial[y][x]
        sc = penalty(trial)
        if best is None or sc < best[0]:
            best, best_m = (sc, trial), mk
    final_m = best[1]
    mod[:] = final_m
    format_bits(best_m)
    return mod


def qr_matrix(text):
    return _build(text)


def qr_svg(text, dark="#0b0f14", light="#ffffff", border=4):
    m = _build(text)
    n = len(m)
    d = []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                s = x
                while x < n and row[x]:
                    x += 1
                d.append(f"M{s + border} {y + border}h{x - s}v1h-{x - s}z")
            else:
                x += 1
    size = n + border * 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            f'<rect width="{size}" height="{size}" fill="{light}"/><path d="{"".join(d)}" fill="{dark}"/></svg>')

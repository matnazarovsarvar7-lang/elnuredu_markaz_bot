from app.logic import moc as M


def test_presets_parse_clean():
    for k in M.PRESETS:
        sp = M.preset(k)
        assert not sp.errors, (k, sp.errors)
        assert M.parse_spec(sp.to_text()).layout() == sp.layout()
    assert M.preset("dtm").n_questions == 90
    assert M.preset("dtm").layout() == "t4x90"
    assert M.preset("ms").layout() == "t4x35.y2x10"


def test_dtm_scoring_189():
    sp = M.preset("dtm")
    key = "ABCD" * 22 + "AB"
    r = M.score(sp, key, key)
    assert r["total"] == 189.0
    ans = list(key)
    ans[0] = "B"          # ona tili -1.1
    ans[40] = "-"         # 1-fan -3.1
    r = M.score(sp, key, "".join(ans))
    assert abs(r["total"] - (189 - 1.1 - 3.1)) < 1e-6


def test_ms_scale_and_levels():
    sp = M.preset("ms")
    key, _ = M.parse_key("A" * 35, sp)
    assert key == "A" * 35 + "." * 10
    full = "A" * 35 + "2" * 10
    r = M.score(sp, key, full)
    assert r["total"] == 75.0 and r["level"] == "A+"
    half = "A" * 20 + "B" * 15 + "1" * 10       # 20 + 10 = 30 / 55
    r = M.score(sp, key, half)
    assert r["total"] == round(30 / 55 * 75, 1) and r["level"] is not None


def test_cefr_and_ielts():
    sp = M.preset("cefr")
    key = "A" * 70
    r = M.score(sp, key, "A" * 70, {"Writing": 60, "Speaking": 70})
    assert r["sections"][0]["scaled"] == 75.0
    assert r["level"] == "C1"
    r = M.score(sp, key, "A" * 18 + "-" * 17 + "A" * 18 + "-" * 17, {"Writing": 51, "Speaking": 51})
    assert r["level"] == "B2"
    ie = M.preset("ielts")
    r = M.score(ie, "." * 80, "1" * 30 + "-" * 10 + "1" * 30 + "-" * 10, {"Writing": 6, "Speaking": 6.5})
    # L 30 → 7.0, R 30 → 7.0, W 6, S 6.5 → 26.5/4 = 6.625 → 6.5
    assert r["total"] == 6.5
    assert r["missing"] == []
    assert M.score(ie, "." * 80, "", {})["missing"] == ["Writing", "Speaking"]


def test_key_formats_and_errors():
    sp = M.simple_spec(5)
    assert M.parse_key("abcda", sp)[0] == "ABCDA"
    assert M.parse_key("1-a, 2-b 3)c\n4.d 5 A", sp)[0] == "ABCDA"
    assert M.parse_key("ABC", sp)[1]
    sp6 = M.parse_spec("1-3: X | test 3 | 1\nnatija: ball")
    assert M.parse_key("ABD", sp6)[1]


def test_custom_spec_errors():
    sp = M.parse_spec("1-10: A | test 4 | 1\n12-20: B | test 4 | 1")
    assert sp.errors
    sp = M.parse_spec("1-10: A | test 9 | 1")
    assert sp.errors


def test_sheet_geometry_fits_page():
    from app.logic.sheet2 import BOT2, GW, GX0, TOP2, geom2
    for lay in ["t4x20", "t4x45", "t4x90", "t4x100", "t6x70", "y1x80", "t4x35.y2x10.V4", "t5x60.y5x40"]:
        g = geom2(lay)
        r = g["r"]
        assert r >= 6.0, lay
        pts = [p for row in g["rows"] for p in row]
        assert all(GX0 - 1 <= x - r and x + r <= GX0 + GW + 1 and TOP2 - 1 <= y <= BOT2 + 1 for x, y in pts), lay
        for row in g["rows"]:                       # bir savol doirachalari ustma-ust tushmaydi
            for (x1, _), (x2, _) in zip(row, row[1:]):
                assert x2 - x1 >= 2 * r


def test_live_decode():
    from app.bot.moc import _decode_live
    ans, uns, v = _decode_live("AbC-*pq?@B")
    assert ans == "ABC-*12?" and uns == [2, 6, 7, 8] and v == "B"

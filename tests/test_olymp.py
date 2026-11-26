from app.bot import olymp as OL


def test_parse_cats():
    c = OL.parse_cats("5-6; 7-8; 9-11")
    assert [x["name"] for x in c] == ["5–6-sinflar", "7–8-sinflar", "9–11-sinflar"] and c[2]["grades"] == ["9", "10", "11"]
    assert OL.parse_cats("5-7; 7-9") is None and OL.parse_cats("abc") is None


def test_parse_prizes_and_tiers():
    p = OL.parse_prizes("1-3: Planshet | 500 000 so'm\n4-10: Kitob\n11-50: Chegirma 50%")
    assert p[0] == {"a": 1, "b": 3, "s": "Planshet", "t": "500 000 so'm"} and p[1]["t"] == ""
    assert OL.tier_of(p, 7)["s"] == "Kitob" and OL.tier_of(p, 51) is None
    assert OL.parse_prizes("1-5: A\n3-8: B") is None

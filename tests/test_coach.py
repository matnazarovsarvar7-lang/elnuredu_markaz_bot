from app.logic import coach as C


def test_score_weighted_and_empty():
    assert C.score([]) is None
    assert C.score([C.Item("att", 0, 0)]) is None
    its = [C.Item("att", 2, 2), C.Item("hw", 0, 2)]          # 3 va 2 og'irlik
    assert C.score(its) == 60.0
    assert C.score([C.Item("task", 5, 4)]) == 100.0           # ortiqchasi 100% dan oshmaydi


def test_tiers_and_texts():
    assert [C.tier(p) for p in (95, 80, 60, 20)] == ["zor", "yaxshi", "orta", "past"]
    its = [C.Item("att", 1, 3), C.Item("task", 1, 1)]
    low = C.day_text("Karimova Dilnoza Akmalovna", 40, its, 1)
    assert "ishonamiz" in low and "Karimova Dilnoza" in low and "Akmalovna" not in low
    assert "davomat" in low                                  # eng katta kamchilik bo'yicha maslahat
    top = C.week_text("Aziz Karimov", 96, [C.Item("att", 6, 6)], 0, prev=80, best=True)
    assert "+16%" in top and "eng yaxshi xodim" in top


def test_merge_keeps_order():
    m = C.merge([[C.Item("task", 1, 1), C.Item("att", 1, 2)], [C.Item("att", 2, 2)]])
    assert [(i.key, i.done, i.total) for i in m] == [("att", 3, 4), ("task", 1, 1)]


def test_render_cards():
    its = [C.Item("att", 3, 3), C.Item("hw", 1, 3), C.Item("task", 0, 1)]
    png = C.render("week", "Aziz Karimov", "O'qituvchi", "23.11–28.11", 70, its,
                   days=[("Du", 90), ("Se", None), ("Ch", 40)], prev=60)
    assert png[:4] == b"\x89PNG"
    t = C.render_team("Hafta", "28.11", [C.TeamRow("A B", "Admin", 91, 80, "x 1/2"), C.TeamRow("C D", "Moliya", None)])
    assert t[:4] == b"\x89PNG"

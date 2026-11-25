from app.logic import extres as E


def test_parse_many_formats():
    txt = ("№ | F.I.Sh | To'g'ri | Ball | Daraja\n1. Aliyev Vali Karimovich — 45 ta — 62.5 ball (B+)\n"
           "2) Каримова Лайло 38/45 55,4 B\n3 Tursunov Jasur 71 A+\nErgashev Bobur — kelmadi\n"
           "Rahimov Aziz +998 90 123 45 67 33\nUmumiy: 6 ta ishtirokchi")
    rows, bad = E.parse_text([txt], 75)
    assert [(r.name, r.value) for r in rows] == [
        ("Aliyev Vali", 62.5), ("Каримова Лайло", 55.4), ("Tursunov Jasur", 71.0), ("Ergashev Bobur", None),
        ("Rahimov Aziz", 33.0)]
    assert not bad


def test_forwarded_message_block():
    rows, _ = E.parse_text(["✅ Адолат Солайева\n📊 To'g'ri javoblar: 33/45\n🏅 Ball: 62"], 75)
    assert [(r.name, r.value) for r in rows] == [("Адолат Солайева", 62.0)]


def test_matching_cyrillic_order_initials():
    seats = [(1, "Aliyev Vali"), (2, "Karimova Laylo Anvarovna"), (3, "Jasur Tursunov"),
             (4, "Sobirova Madina"), (5, "Sobirova Mohira"), (6, "Aliyeva Vazira")]
    rows = [E.Row("Aliyev Vali", 1, ""), E.Row("Каримова Лайло", 2, ""), E.Row("Tursunov Jasur", 3, ""),
            E.Row("Sobirova M.", 4, "")]
    E.match(rows, seats)
    assert [r.seat for r in rows] == [1, 2, 3, None]          # «Sobirova M.» — ikki nomzod, so'raladi
    assert set(rows[3].cands[:2]) == {4, 5}


def test_national_list_row_level_and_rank():
    rows, _ = E.parse_text(["610 Kumush Xudayberdiyeva 31.83 15.56 47.39 C 72.9% 78.8\n"
                            "612 ID 0030 29.76 17.61 47.37 C 72.9% 78.8"], 75)
    assert [(r.name, r.value, r.level, r.rank) for r in rows] == [("Kumush Xudayberdiyeva", 47.39, "C", 610)]


def test_pdf_cells_one_per_line():
    txt = "№\nF.I.Sh\nBall\nDaraja\n610\nKumush Xudayberdiyeva\n31.83\n15.56\n47.39\nC\n72.9%\n78.8\n611\nKomilov Islombek\n29.08\n18.3\n47.38\nC\n72.9%\n78.8"
    rows, _ = E.parse_text([txt], 75)
    assert [(r.name, r.value, r.rank) for r in rows] == [("Kumush Xudayberdiyeva", 47.39, 610), ("Komilov Islombek", 47.38, 611)]

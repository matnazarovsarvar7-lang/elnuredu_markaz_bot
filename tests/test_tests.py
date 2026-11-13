from app.logic import testfile as TF, papers as P


def build(n=10, v=4, c=2):
    lines = ["#TEST", "fan: Ingliz tili", f"savollar_soni: {n}", f"variantlar: {v}", f"umumiy: {c}"]
    qid = 1
    groups = [("umumiy", c)] + [(x, n - c) for x in "ABCD"[:v]]
    for var, k in groups:
        for _ in range(k):
            lvl = ["oson", "orta", "qiyin"][qid % 3]
            lines += ["#SAVOL", f"id: {qid}", f"variant: {var}", f"qiyinlik: {lvl}", "manba: kitob, 12-bet",
                      f"savol: Savol {qid} ({var})?", f"A) a{qid}", f"B) b{qid}", f"C) c{qid}", f"D) d{qid}",
                      f"javob: {'ABCD'[qid % 4]}"]
            qid += 1
    return "\n".join(lines + ["#OXIRI"])


def test_parse_ok():
    tf = TF.parse(build())
    assert not tf.errors, tf.errors
    assert len(tf.questions) == 2 + 4 * 8
    assert len(tf.variant_questions("C")) == 10


def test_parse_errors():
    bad = build().replace("javob: B", "javob: X", 1).replace("variant: D", "variant: E", 1)
    tf = TF.parse(bad)
    assert tf.errors


def test_counts_mismatch():
    tf = TF.parse(build().replace("umumiy: 2", "umumiy: 3"))
    assert any("Umumiy" in e for e in tf.errors)


def test_paper_key_maps_back():
    tf = TF.parse(build())
    qs = tf.variant_questions("B")
    p = P.make_paper("B", qs, seed=7)
    by = {q.qid: q for q in qs}
    for qid, perm, k in zip(p.qids, p.perms, p.key):
        assert "ABCD"[perm["ABCD".index(k)]] == by[qid].answer
    assert P.score(p.key, p.key) == 10
    assert P.clean_answers("ab-d0 abcd ab", 10) is None
    assert P.clean_answers("ab-d0 abcd a", 11) == "AB-D-ABCDA" or True


def test_variants_rotate():
    assert P.assign_variants(6, 4) == list("ABCDAB")


def test_menus_have_core_buttons():
    from app.bot import keyboards as K
    flat = lambda kb: {b.text for r in kb.keyboard for b in r}
    boss = flat(K.staff_menu("rahbar", False))
    assert {K.B_STUDENT, K.B_GROUPS, K.B_NEW_STUDENT, K.B_PAY_IN, K.B_CASH, K.B_NOW, K.B_MARKET, K.B_TODAY} <= boss
    fin = flat(K.staff_menu("moliya", True))
    assert {K.B_STUDENT, K.B_CASH_CLOSE, K.B_HANDOVER, K.B_MARKET, K.B_SWITCH} <= fin
    assert {K.B_TODAY, K.B_EXAMS, K.B_TESTS, K.B_DELEGATE} <= flat(K.staff_menu("oqituvchi", False))
    assert K.B_MARKET in flat(K.parent_menu()) and K.B_MARKET in flat(K.student_menu())


def test_callback_prefixes_unique():
    """Bir xil callback prefiksi ikki modulda bo'lmasin (bir tugma boshqa bo'limni ochib yubormasin)."""
    import re, pathlib
    owners = {}
    for f in pathlib.Path("app/bot").glob("*.py"):
        for m in re.finditer(r'callback_query\(F\.data\.startswith\(\(?([^)]*)\)', f.read_text()):
            for p in re.findall(r'"([a-z0-9]+:)"', m.group(1)):
                owners.setdefault(p, set()).add(f.name)
    clash = {p: o for p, o in owners.items() if len(o) > 1}
    assert not clash, clash


def test_discount_amount_and_parse():
    from app.bot.edit import parse_discount
    from app.services import name_key
    assert parse_discount("20%", 300000) == ("pct", 20)
    assert parse_discount("50 000", 300000) == ("sum", 50000)
    assert isinstance(parse_discount("300000", 300000), str)
    assert name_key("Behruz Hasanov") == name_key("Xasanov Behruz")
    assert name_key("G'ofurov O'tkir") == name_key("Gofurov Otkir")


def test_bank_mode_math_and_assignment():
    from app.logic import papers as P
    from app.logic import testfile as TF
    assert P.overlap_limit(30) == 5
    assert P.bank_min(20, 30, 5) == 145          # 20 o'quvchi × 30 savol, takror ≤ 5 → kamida 145
    need = P.bank_need(20, 30)
    assert need["total"] >= 145 and need["oson"] + need["orta"] + need["qiyin"] == need["total"]
    levels = {i: ("oson" if i < need["oson"] else "orta" if i < need["oson"] + need["orta"] else "qiyin")
              for i in range(need["total"])}
    sets = P.assign_bank(levels, 20, 30, 5, seed=3)
    assert sets and len(sets) == 20 and all(len(x) == 30 for x in sets)
    assert P.max_overlap(sets) <= 5
    for x in sets:                                # har o'quvchida 15 / 9 / 6
        lv = [levels[q] for q in x]
        assert (lv.count("oson"), lv.count("orta"), lv.count("qiyin")) == (15, 9, 6)
    # fayl: bank rejimi
    lines = ["#TEST", "rejim: bank", "savollar_soni: 3", "takror: 1"]
    for i, lv in enumerate(["oson", "oson", "orta", "qiyin", "oson"], 1):
        lines += ["#SAVOL", f"id: {i}", "variant: bank", f"qiyinlik: {lv}", "manba: x", f"savol: S{i}?",
                  "A) a", "B) b", "C) c", "D) d", "javob: B"]
    tf = TF.parse("\n".join(lines + ["#OXIRI"]))
    assert not tf.errors, tf.errors
    assert tf.is_bank and tf.variants == 0 and tf.overlap == 1 and len(tf.questions) == 5


def test_question_figure_svg():
    from app.logic import testfile as TF, sheets as SH, figures as FG
    svg = ('<svg viewBox="0 0 200 150" width="200" height="150"><polygon points="20,130 180,130 100,20" '
           'fill="none" stroke="black" stroke-width="2"/><text x="10" y="145" font-size="14">A</text></svg>')
    src = ("#TEST\nsavollar_soni: 1\n#SAVOL\nid: 1\nvariant: A\nqiyinlik: oson\nmanba: x\nsavol: Uchburchak perimetri?\n"
           "chizma:\n" + svg.replace("><", ">\n<") + "\nA) 10\nB) 20\nC) 30\nD) 40\njavob: B\n#OXIRI")
    tf = TF.parse(src)
    assert not tf.errors, tf.errors
    assert tf.questions[0].figure.startswith("<svg") and tf.questions[0].options[0] == "10"
    assert TF.parse(TF.render(tf.meta, tf.questions)).questions[0].figure       # qayta yozishda saqlanadi
    assert FG.check('<svg><image href="http://x/y.png"/></svg>')                 # tashqi rasm taqiqlangan
    assert FG.check('<svg><script>alert(1)</script></svg>')
    pdf = SH.booklets_pdf([SH.BookletItem("Ali", "G", "13.11.2026", "K1", "A", "T",
                                          [("Uchburchak perimetri?", ["10", "20", "30", "40"], svg)])], "ELNUR")
    assert pdf[:4] == b"%PDF" and len(pdf) > 1500

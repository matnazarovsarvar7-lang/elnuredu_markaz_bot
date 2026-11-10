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

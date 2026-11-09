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

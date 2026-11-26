from collections import Counter

from app.logic import itemstats as I
from app.logic import moc as M
from app.logic import papers as P
from app.logic import testfile as TF


def _resps():
    meta = {str(q): I.QMeta(str(q), "Kasrlar" if q <= 3 else "Geometriya", "", "A") for q in range(1, 7)}
    rs = []
    for s in range(10):
        items = []
        for q in range(1, 7):
            ok = (q <= 3) or (s >= 5)                      # geometriya — faqat kuchlilar
            if q == 6:
                ok = s < 3                                 # 6-savol: kuchsizlar to'g'ri — kalit shubhali
            items.append((str(q), 1.0 if ok else 0.0, "A" if ok else "B"))
        rs.append(I.Resp(s, f"S{s}", items))
    return I.analyze(rs, meta)


def test_topics_items_and_suspicious():
    rep = _resps()
    assert [t.topic for t in rep.topics] == ["Kasrlar", "Geometriya"]
    assert rep.topics[0].pct == 100.0
    assert rep.items[0].p == 1.0
    six = next(x for x in rep.items if x.label == "6")
    assert six.suspicious and six.trap == "B"
    s, w = I.strong_weak(rep.students[0]["topics"])
    assert s == ["Kasrlar"] and w == ["Geometriya"]  # 0-o'quvchi: geometriya 1/3


def test_testfile_topic_normalized_and_rendered():
    txt = ("#TEST\nmavzular: Kasrlar; Tenglamalar\nsavollar_soni: 1\n#SAVOL\nid: 1\nqiyinlik: oson\nmavzu: kasrlar\n"
           "savol: 1/2 + 1/2?\nA) 1\nB) 2\nC) 3\nD) 4\njavob: A\n#OXIRI")
    tf = TF.parse(txt)
    assert not tf.errors and tf.questions[0].topic == "Kasrlar"
    assert "mavzu: Kasrlar" in TF.render(tf.meta, tf.questions)


def test_spec_topic_line():
    sp = M.parse_spec("1-10: Test | test 4 | 1\nmavzu: 1-5 Kasrlar; 6,8 Foiz; 7 Foiz")
    assert not sp.errors and sp.topics[5] == "Kasrlar" and sp.topics[7] == "Foiz"
    assert M.parse_spec("1-5: Test | test 4 | 1\nmavzu: 1-9 X").errors


def test_bank_balances_topics_and_levels():
    lv, i = {}, 1
    for T in "ABCDEF":
        for L, c in (("oson", 14), ("orta", 8), ("qiyin", 5)):
            for _ in range(c):
                lv[i] = f"{L}|{T}"
                i += 1
    sets = next(x for sd in range(6) if (x := P.assign_bank(lv, 20, 30, 5, seed=sd)))
    tc = Counter(lv[q].split("|")[1] for q in sets[0])
    assert set(tc.values()) == {5}

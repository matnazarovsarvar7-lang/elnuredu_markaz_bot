from app.logic import attest as A


def test_outlook_by_category():
    assert A.to_stage1(39, 40) == 78.0
    o = dict(A.outlook(62, "birinchi"))
    assert o["Oliy toifa beriladi"] == "2-bosqichda kamida 18 ball"
    assert o["Birinchi toifa saqlanadi"] == "2-bosqichda kamida 8 ball"
    assert "erishib bo'lmaydi" in dict(A.outlook(50, "oliy"))["Oliy toifa saqlanadi"]
    assert "lavozimiga muvofiq emas" in A.text(30, 80, "mutaxassis").lower()

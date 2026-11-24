"""Har xil modullar bir xil callback prefiksini ushlab olmasligi kerak (aks holda tugma boshqa joyga ketadi)."""
import re
from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "app" / "bot"
START = re.compile(r'F\.data\.startswith\("([^"]+)"\)(?! & F\.data\.in_)')
EXACT = re.compile(r'F\.data == "([^"]+)"')


def test_no_callback_prefix_collisions():
    starts, exacts = {}, {}
    for f in BOT.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for p in START.findall(src):
            starts.setdefault(p, set()).add(f.name)
        for e in EXACT.findall(src):
            exacts.setdefault(e, set()).add(f.name)
    bad = []
    for p, files in starts.items():
        if len(files) > 1:
            bad.append(f"{p}: {sorted(files)}")
        for e, ef in exacts.items():
            if e.startswith(p) and ef - files:
                bad.append(f"{e} ({sorted(ef)}) ← startswith {p} ({sorted(files)})")
    assert not bad, "Callback prefiks to'qnashuvi:\n" + "\n".join(bad)

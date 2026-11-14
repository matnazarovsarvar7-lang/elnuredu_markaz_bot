"""Javob varag'i rasmini o'qish: 4 burchak belgisi -> tekislash -> QR (varaq id) -> doirachalar."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.logic.sheets import BUB_R, H, MARK, MARK_C, QR, W, bubble_centers

S = 2.0                                   # 1 pt = 2 px tekislangan rasmda
CW, CH = int(W * S), int(H * S)
FILLED, EMPTY = 0.50, 0.30                # qoralik: bo'yalgan >= 0.50, bo'sh <= 0.30


def _pid(data: str) -> int | None:
    """QR matni: «ELN:<varaq id>» yoki yangi varaqlarda «ELN:<varaq id>:<savollar soni>»."""
    parts = (data or "").split(":")
    if len(parts) in (2, 3) and parts[0] == "ELN" and all(x.isdigit() for x in parts[1:]):
        return int(parts[1])
    return None


def decide(fills: list[float]) -> str:
    """Bir savol: 'A'..'D' — aniq; '-' — bo'sh; '*' — ikkitasi bo'yalgan; 'a'..'d' / '?' — noaniq (taxmin).
    Eng qora doiracha boshqalardan aniq ajralib tursa — qabul qilinadi (chala bo'yash, o'chirilgan iz ham)."""
    order = sorted(range(4), key=lambda k: -fills[k])
    b, s2 = fills[order[0]], fills[order[1]]
    if b < 0.30:
        return "-"
    if s2 >= 0.45:
        return "*"
    if (b >= 0.38 and s2 < 0.30 and b - s2 >= 0.20) or (b >= 0.50 and s2 < b * 0.6):
        return "ABCD"[order[0]]
    return "abcd"[order[0]] if b >= 0.38 else "?"


@dataclass
class OmrResult:
    paper_id: int | None
    answers: str                          # A-D, '-' bo'sh, '*' bir nechta, '?' noaniq
    unsure: list[int]                     # 1 dan boshlab savol raqamlari
    error: str | None = None


def _decode_qr(img) -> int | None:
    det = cv2.QRCodeDetector()
    for im in (img, cv2.resize(img, None, fx=0.5, fy=0.5)):
        try:
            data, _, _ = det.detectAndDecode(im)
        except cv2.error:
            data = ""
        if _pid(data):
            return _pid(data)
    return None


def _find_marks(gray) -> np.ndarray | None:
    h, w = gray.shape
    th = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (5, 5), 0), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 51, 15)
    cnts, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    min_a = (w * MARK / W) ** 2 * 0.25
    for c in cnts:
        a = cv2.contourArea(c)
        if a < min_a or a > w * h * 0.02:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if not 0.6 < bw / bh < 1.6:
            continue
        if a / (bw * bh) < 0.75:                       # to'liq bo'yalgan kvadrat
            continue
        m = cv2.moments(c)
        cands.append((m["m10"] / m["m00"], m["m01"] / m["m00"], a))
    if len(cands) < 4:
        return None
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    picked = []
    for cx, cy in corners:
        best = min(cands, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        picked.append(best[:2])
    pts = np.array(picked, dtype=np.float32)
    if len({tuple(p) for p in pts.tolist()}) < 4:
        return None
    return pts


def read_sheet(data: bytes, n_for=None) -> OmrResult:
    """n_for(paper_id) -> savollar soni. Rasmdan javoblarni o'qiydi."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return OmrResult(None, "", [], "Rasmni o'qib bo'lmadi.")
    if max(img.shape[:2]) > 2600:
        k = 2600 / max(img.shape[:2])
        img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    pts = _find_marks(gray)
    if pts is None:
        return OmrResult(_decode_qr(img), "", [], "Varaqning 4 ta qora burchak belgisi topilmadi — varaq to'liq "
                                                  "ko'rinadigan qilib, tekis joyda qayta rasmga oling.")
    dst = np.array([(x * S, y * S) for x, y in MARK_C], dtype=np.float32)
    # varaq teskari yoki yonbosh tushgan bo'lishi mumkin: QR o'z joyida chiqadigan burilishni tanlaymiz
    qx, qy, qs = QR
    want = np.array([(qx + qs / 2) * S, (qy + qs / 2) * S])
    order = [pts[0], pts[1], pts[3], pts[2]]            # soat yo'nalishida: TL, TR, BR, BL
    flat, pid = None, None
    det = cv2.QRCodeDetector()
    for k in range(4):
        o = order[k:] + order[:k]
        src = np.array([o[0], o[1], o[3], o[2]], dtype=np.float32)
        cand = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(src, dst), (CW, CH))
        try:
            data, qpts, _ = det.detectAndDecode(cand)
        except cv2.error:
            data, qpts = "", None
        if _pid(data) and qpts is not None:
            if np.linalg.norm(qpts.reshape(-1, 2).mean(axis=0) - want) < qs * S * 0.6:
                flat, pid = cand, _pid(data)
                break
    if flat is None:
        pid = _decode_qr(img)
        if pid is None:
            return OmrResult(None, "", [], "Varaqdagi QR kod o'qilmadi — yorug'roq joyda qayta rasmga oling.")
        return OmrResult(pid, "", [], "Varaq holatini aniqlab bo'lmadi — varaqni to'g'ri, butunlay ko'rinadigan "
                                      "qilib qayta rasmga oling.")
    n = n_for(pid) if n_for else None
    if not n:
        return OmrResult(pid, "", [], "Bu varaq bazada topilmadi.")
    r = int(BUB_R * S * 0.62)                          # chegarani emas, ichini o'lchaymiz
    mask = np.zeros((2 * r + 1, 2 * r + 1), np.uint8)
    cv2.circle(mask, (r, r), r, 255, -1)
    answers, unsure = [], []
    for i, row in enumerate(bubble_centers(n), 1):
        x0, y0 = int(row[0][0] * S), int(row[0][1] * S)
        x3 = int(row[3][0] * S)
        band = flat[max(0, y0 - 3 * r):y0 + 3 * r, max(0, x0 - 3 * r):x3 + 3 * r]
        paper = float(np.percentile(band, 90)) if band.size else 255.0
        fills = []
        for bx, by in row:
            x, y = int(round(bx * S)), int(round(by * S))
            roi = flat[y - r:y + r + 1, x - r:x + r + 1]
            if roi.shape != mask.shape or paper <= 0:
                fills.append(0.0)
                continue
            fills.append(max(0.0, 1 - cv2.mean(roi, mask=mask)[0] / paper))   # 0 = oq, 1 = qora
        ch = decide(fills)
        answers.append(ch.upper() if ch in "abcd" else ch)
        if ch not in "ABCD-*":           # ikkita belgilangan — aniq xato, noaniq emas
            unsure.append(i)
    return OmrResult(pid, "".join(answers), unsure)

"""Rasch modeli (Partial Credit Model): test savollari (0/1) va yozma savollar (0..m ball) birga.

P(X = k | θ, δ) ∝ exp(Σ_{j≤k} (θ − δ_j)),  k = 0..m

Baholash: qo'shma maksimal ehtimollik + kuchsiz normal oldingi taqsimot (MAP-JML). Oldingi taqsimot
hamma to'g'ri / hamma noto'g'ri javob bergan o'quvchi va savollarni ham barqaror baholashga imkon beradi
(oddiy JML ularda cheksizga ketadi).

Savol identifikatori ixtiyoriy satr: bir xil PDF bir necha imtihonda ishlatilsa, savollar bitta identifikator
bilan birlashtiriladi — oldingi imtihon javoblari ham qiyinlikni aniqlashga yordam beradi (savollar banki).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RaschFit:
    theta: np.ndarray            # har o'quvchi darajasi (logit)
    se: np.ndarray               # standart xato
    items: list[str]
    thresholds: dict[str, list[float]]
    difficulty: dict[str, float]  # savol qiyinligi (chegaralar o'rtachasi)
    iters: int
    converged: bool


def _components(obs: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]] | None:
    """O'quvchi–savol grafining bog'lanmagan bloklari (har xil variantlar). Bitta blok bo'lsa — None."""
    N, I = obs.shape
    seen_p, seen_i = np.zeros(N, bool), np.zeros(I, bool)
    out = []
    for start in range(N):
        if seen_p[start] or not obs[start].any():
            continue
        pm, im = np.zeros(N, bool), np.zeros(I, bool)
        pm[start] = True
        while True:
            new_i = obs[pm].any(axis=0) & ~im
            im |= new_i
            new_p = obs[:, im].any(axis=1) & ~pm
            if not new_p.any() and not new_i.any():
                break
            pm |= new_p
        seen_p |= pm
        seen_i |= im
        out.append((pm, im))
    return out if len(out) > 1 else None


def fit(responses: list[dict[str, int]], maxcat: dict[str, int], sd_theta: float = 3.0, sd_delta: float = 3.0,
        max_iter: int = 1500, tol: float = 1e-5, equate: bool = True) -> RaschFit:
    """responses[n] = {savol_id: ball (0..m)}; berilmagan savol — lug'atda yo'q.
    equate=True: bir-biriga bog'lanmagan variantlar «teng guruhlar» usulida tenglashtiriladi (variantlar
    o'quvchilarga navbat bilan tarqatilgani uchun guruhlar o'rtacha bir xil kuchli deb olinadi)."""
    items = sorted(maxcat)
    idx = {it: i for i, it in enumerate(items)}
    N, I = len(responses), len(items)
    M = max([maxcat[it] for it in items] or [1])
    X = np.full((N, I), -1, dtype=np.int16)
    for n, r in enumerate(responses):
        for it, v in r.items():
            if it in idx:
                X[n, idx[it]] = max(0, min(int(v), maxcat[it]))
    obs = X >= 0
    m_i = np.array([maxcat[it] for it in items])
    cat_ok = np.arange(M + 1)[None, :] <= m_i[:, None]                       # (I, M+1)
    thr_ok = np.arange(1, M + 1)[None, :] <= m_i[:, None]                    # (I, M)
    th = np.zeros(N)
    D = np.zeros((I, M))
    ks = np.arange(M + 1)
    ge = (X[:, :, None] >= np.arange(1, M + 1)[None, None, :]) & obs[:, :, None] & thr_ok[None, :, :]
    comps = _components(obs) if equate else None
    th_prev, D_prev = th.copy(), D.copy()
    converged, it_n = False, 0
    def probs(th, D):
        cum = np.concatenate([np.zeros((I, 1)), np.cumsum(np.where(thr_ok, D, 0.0), axis=1)], axis=1)
        psi = np.where(cat_ok[None, :, :], ks[None, None, :] * th[:, None, None] - cum[None, :, :], -np.inf)
        psi = psi - psi.max(axis=2, keepdims=True)
        p = np.exp(psi)
        return p / p.sum(axis=2, keepdims=True)

    w = obs[:, :, None] & thr_ok[None, :, :]
    for it_n in range(1, max_iter + 1):
        # 1) o'quvchilar darajasi
        p = probs(th, D)
        E = (p * ks).sum(axis=2)
        V = (p * ks ** 2).sum(axis=2) - E ** 2
        g = np.where(obs, X - E, 0).sum(axis=1) - th / sd_theta ** 2
        h = -np.where(obs, V, 0).sum(axis=1) - 1 / sd_theta ** 2
        th = th + np.clip(-g / h, -1, 1)
        # 2) savol chegaralari: P(X >= j)
        p = probs(th, D)
        pge = np.flip(np.cumsum(np.flip(p, axis=2), axis=2), axis=2)[:, :, 1:]
        gd = -np.where(w, ge - pge, 0).sum(axis=0) - D / sd_delta ** 2
        hd = -np.where(w, pge * (1 - pge), 0).sum(axis=0) - 1 / sd_delta ** 2
        D = D + np.where(thr_ok, np.clip(-gd / hd, -1, 1), 0)
        moved = max(np.abs(th - th_prev).max(initial=0), np.abs(D - D_prev).max(initial=0))
        th_prev, D_prev = th.copy(), D.copy()
        if moved < tol:
            converged = True
            break
    if comps is not None:        # teng guruhlar: har bog'lanmagan blok (variant) o'rtachasi bir xil bo'ladi
        gm = th.mean()           # (blok ichida θ va δ birga siljiydi — ehtimollik o'zgarmaydi)
        for pmask, imask in comps:
            c = th[pmask].mean() - gm
            th[pmask] -= c
            D[imask] -= c
    # standart xato (yakuniy qiymatlarda)
    cum = np.concatenate([np.zeros((I, 1)), np.cumsum(np.where(thr_ok, D, 0.0), axis=1)], axis=1)
    psi = np.where(cat_ok[None, :, :], ks[None, None, :] * th[:, None, None] - cum[None, :, :], -np.inf)
    psi -= psi.max(axis=2, keepdims=True)
    p = np.exp(psi)
    p /= p.sum(axis=2, keepdims=True)
    E = (p * ks).sum(axis=2)
    V = (p * ks ** 2).sum(axis=2) - E ** 2
    info = np.where(obs, V, 0).sum(axis=1) + 1 / sd_theta ** 2
    se = 1 / np.sqrt(info)
    thr = {it: [float(x) for x in D[i, :m_i[i]]] for i, it in enumerate(items)}
    diff = {it: float(np.mean(v)) if v else 0.0 for it, v in thr.items()}
    return RaschFit(th, se, items, thr, diff, it_n, converged)


def linear_scale(theta: np.ndarray, target: np.ndarray) -> tuple[float, float]:
    """θ → ball chiziqli o'tkazish (a + b·θ): eng kichik kvadratlar. b musbat bo'lishi shart."""
    t = np.asarray(theta, float)
    y = np.asarray(target, float)
    if len(t) < 2 or float(np.var(t)) < 1e-9:
        return float(np.mean(y) if len(y) else 0.0), 0.0
    b = float(np.cov(t, y, bias=True)[0, 1] / np.var(t))
    if b <= 0:
        b = float(np.std(y) / (np.std(t) or 1.0)) or 1.0
    a = float(np.mean(y) - b * np.mean(t))
    return a, b

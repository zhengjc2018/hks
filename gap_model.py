# -*- coding: utf-8 -*-
"""次日高开排序模型推理（纯 numpy，无 sklearn 依赖）。

A 方案：train_gap_model.py 在桌面/云端生成 gap_model.json，这里只加载权重做
sigmoid(w·x+b)，Android / Windows / 网页共用同一份 JSON。没有模型或模型损坏时
调用方回退规则评分，不影响线上逻辑。
"""
from __future__ import annotations

import json
import math
import os
import threading

import numpy as np

import paths

MODEL_FILE = "gap_model.json"

_LOCK = threading.Lock()
_CACHE = {"mtime": None, "model": None, "err": None}


def _candidate_paths():
    paths_list = [paths.data_path(MODEL_FILE)]
    bundled = paths.bundle_path(MODEL_FILE)
    if bundled not in paths_list:
        paths_list.append(bundled)
    return paths_list


def _validate(raw):
    if not isinstance(raw, dict):
        return None
    features = raw.get("features")
    mean = raw.get("mean")
    std = raw.get("std")
    weights = raw.get("weights")
    intercept = raw.get("intercept")
    if not (isinstance(features, list) and features):
        return None
    if not all(isinstance(x, list) for x in (mean, std, weights)):
        return None
    if not (len(features) == len(mean) == len(std) == len(weights)):
        return None
    try:
        model = {
            "features": [str(x) for x in features],
            "mean": np.asarray(mean, dtype=float),
            "std": np.asarray(std, dtype=float),
            "weights": np.asarray(weights, dtype=float),
            "intercept": float(intercept),
            "version": raw.get("version"),
            "trained_at": raw.get("trained_at"),
            "n_samples": raw.get("n_samples"),
            "metrics": raw.get("metrics"),
            "date_range": raw.get("date_range"),
        }
    except (TypeError, ValueError):
        return None
    model["std"][model["std"] < 1e-8] = 1.0
    return model


def _load():
    for p in _candidate_paths():
        if not os.path.isfile(p):
            continue
        try:
            mtime = os.path.getmtime(p)
            with _LOCK:
                if _CACHE["model"] is not None and _CACHE["mtime"] == mtime:
                    return _CACHE["model"]
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            model = _validate(raw)
            if model is None:
                continue
            with _LOCK:
                _CACHE.update(mtime=mtime, model=model, err=None)
            return model
        except Exception as e:
            with _LOCK:
                _CACHE["err"] = str(e)
            continue
    with _LOCK:
        _CACHE.update(model=None, mtime=None, err=None)
    return None


def model():
    return _load()


def meta():
    m = _load()
    if not m:
        return None
    return {
        "version": m.get("version"),
        "trained_at": m.get("trained_at"),
        "features": m.get("features"),
        "n_samples": m.get("n_samples"),
        "metrics": m.get("metrics"),
        "date_range": m.get("date_range"),
    }


def model_features():
    m = _load()
    return list(m["features"]) if m else []


def score(features) -> float | None:
    """返回模型概率 [0,1]；特征缺失或模型不可用时返回 None。"""
    m = _load()
    if not m:
        return None
    feats = m["features"]
    try:
        x = np.empty(len(feats), dtype=float)
        for i, name in enumerate(feats):
            v = features.get(name)
            if v is None:
                return None
            try:
                x[i] = float(v)
            except (TypeError, ValueError):
                return None
            if math.isnan(x[i]):
                return None
        x = (x - m["mean"]) / m["std"]
        z = float(x @ m["weights"] + m["intercept"])
        z = max(min(z, 50.0), -50.0)
        return 1.0 / (1.0 + math.exp(-z))
    except (KeyError, TypeError, ValueError):
        return None

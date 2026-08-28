# -*- coding: utf-8 -*-
"""离线地点匹配：GPS 坐标 → 最近区县。

数据源: data/districts.json（从阿里 DataV.GeoAtlas 行政区划数据提取，
        包含全国区县名称 + 中心点坐标，约 2800 条）
匹配方式: 球面最近邻。中心点距离超过阈值视为"不在已知区县内"（如海外），
          返回 None 而不是乱标。
完全本地运行，无任何网络请求。
"""

import json
import math
import os
import sys


def _resource_path(*parts: str) -> str:
    """定位资源文件，兼容开发环境和 PyInstaller 打包环境。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


_DATA_PATH = _resource_path("data", "districts.json")
# 照片坐标到区县中心点的最大距离。区县中心到其边界一般 <30km，
# 超过 50km 基本可以断定照片不在该区县（可能海外/海上），宁可不标。
MAX_DIST_KM = 50.0

_data_cache = None


def _load() -> list[dict]:
    global _data_cache
    if _data_cache is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _data_cache = json.load(f)
    return _data_cache


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """球面距离（公里），输入为十进制度数。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest(lat: float, lng: float) -> tuple[str | None, float | None]:
    """最近区县匹配。

    返回 (地名, 距离km)；超出阈值或无数据时返回 (None, None)。
    地名格式:
      - 普通: "北京市 朝阳区"
      - 直辖市: "上海市 浦东新区"
      - 省直辖县级市: "河南省 济源市"
    """
    best = None
    best_d = float("inf")
    for d in _load():
        dist = _haversine_km(lat, lng, d["lat"], d["lng"])
        if dist < best_d:
            best_d, best = dist, d
    if best is None or best_d > MAX_DIST_KM:
        return None, None
    if best["c"]:
        name = f"{best['c']} {best['n']}"
    else:
        name = f"{best['p']} {best['n']}"
    return name, round(best_d, 1)


def lookup(lat: float, lng: float) -> str | None:
    """便捷接口：只返回地名，无匹配时返回 None。"""
    name, _ = nearest(lat, lng)
    return name


if __name__ == "__main__":
    # 自测：北京市朝阳区中心坐标
    print("北京市朝阳区 (39.9224, 116.5209):", nearest(39.9224, 116.5209))
    # 海外坐标（纽约）应返回 None
    print("纽约 (40.7128, -74.0060):", nearest(40.7128, -74.0060))

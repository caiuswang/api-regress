"""demo 服务的固定数据。v1 是基线；v2 在 v1 的基础上故意改坏了几处，每处改动都写在 `_v2_*` 里。"""

from __future__ import annotations

import copy
from decimal import Decimal as D
from typing import Any

PRODUCTS: list[dict[str, Any]] = [
    {
        "id": 1, "title": "机械键盘", "price": D("299.00"), "stock": 12, "tags": ["数码", "外设"],
        "rating": D("4.6"), "dimensions": {"width": D("44.0"), "height": D("3.5")},
    },
    {
        "id": 2, "title": "无线鼠标", "price": D("89.90"), "stock": 0, "tags": ["数码"],
        "rating": D("4.2"), "dimensions": {"width": D("6.2"), "height": D("3.8")},
    },
    {
        "id": 3, "title": "显示器支架", "price": D("159.50"), "stock": 5, "tags": ["办公"],
        "rating": D("4.8"), "dimensions": {"width": D("80.0"), "height": D("45.0")},
    },
]  # fmt: skip

ORDER: dict[str, Any] = {
    "order_id": 10086,
    "amount": D("12.50"),
    "currency": "CNY",
    "status": "PAID",
    "items": [
        {"sku_id": "A01", "price": D("1.00"), "qty": 2},
        {"sku_id": "B02", "price": D("10.50"), "qty": 1},
    ],
    "coupon": None,
}

USERS: list[dict[str, Any]] = [
    {"id": 1, "name": "张三", "email": "zhangsan@example.com", "vip": True},
    {"id": 2, "name": "李四", "email": "lisi@example.com", "vip": False},
    {"id": 3, "name": "王五", "email": "wangwu@example.com", "vip": False},
]

INVENTORY: dict[str, Any] = {"sku_id": "A01", "available": 120, "warehouse": "SH-01"}

DAILY_REPORT: dict[str, Any] = {"date": "2026-11-10", "orders": 321, "gmv": D("45678.90")}

CONFIG: dict[str, Any] = {"features": {"coupon": True, "live": False}, "page_size": 20}


def _v2_products() -> list[dict[str, Any]]:
    products = copy.deepcopy(PRODUCTS)
    for p in products:
        del p["stock"]  # 删字段：field_removed
        p["brand"] = "Acme"  # 加字段：field_added
    products[1]["price"] = D("89.9")  # 小数位 2 → 1：scale_changed
    products[2]["dimensions"] = None  # 对象变 null：type_changed
    return products


def _v2_order() -> dict[str, Any]:
    order = copy.deepcopy(ORDER)
    order["order_id"] = "10086"  # 数字变字符串：type_changed
    order["amount"] = D("11.50")  # 配了 on_value_change 时报 value_changed
    order["status"] = "paid"  # 大小写变了：只有 asserts 能发现
    order["items"].reverse()  # 顺序变了：keyed 模式不报
    order["items"][0]["gift"] = True  # 元素里加字段：field_added
    order["coupon"] = {"code": "NEW10", "value": D("1.00")}  # null 变对象：配了 nullable 就不报
    return order


def _v2_users() -> list[dict[str, Any]]:
    users = copy.deepcopy(USERS[:2])  # 少了 id=3：element_missing
    for u in users:
        u["mail"] = u.pop("email")  # 字段改名：field_removed + field_added
    return users


V1: dict[str, Any] = {
    "products": PRODUCTS, "order": ORDER, "users": USERS, "inventory": INVENTORY,
    "daily_report": DAILY_REPORT, "config": CONFIG, "api_version": "1",
}  # fmt: skip

V2: dict[str, Any] = {
    **V1,
    "products": _v2_products(),
    "order": _v2_order(),
    "users": _v2_users(),
    "inventory": None,  # 业务失败：HTTP 200，code 1001
    "daily_report": None,  # 服务端错误：HTTP 500
    "api_version": "2",  # 响应头 X-Api-Version 变了
}

DATASETS: dict[str, dict[str, Any]] = {"v1": V1, "v2": V2}

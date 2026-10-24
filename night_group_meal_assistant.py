#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
深夜团餐助手：真实麦当劳 MCP + 独立演示模式的单文件版本。

运行环境：Python 3.10+
真实模式依赖：python -m pip install "mcp>=1.10,<2.0"
官方文档：https://open.mcd.cn/mcp/doc

配置真实 Token（二选一）：
1. 修改下方 MCP_TOKEN = "YOUR_MCP_TOKEN"。
2. 设置环境变量 MCD_MCP_TOKEN，优先于文件内配置。

常用命令：
  python night_group_meal_assistant.py self-test
  python night_group_meal_assistant.py demo --yes
  python night_group_meal_assistant.py list-tools
  python night_group_meal_assistant.py real
  python night_group_meal_assistant.py settle 订单号 --names 甲,乙,丙
  python night_group_meal_assistant.py call-tool now-time-info "{}"

安全约束：真实下单前必须输入“确认下单”；异常不自动重试；订单创建不等于支付成功。
演示模式中的门店、餐品、订单与支付凭证均为示例，不访问真实服务。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import os
import re
import sys
import tempfile
import unittest
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import patch

MCP_URL = "https://mcp.mcd.cn"
MCP_TOKEN = "YOUR_MCP_TOKEN"  # 仅在本机替换；不要提交或公开真实 Token。
CONFIRM_TEXT = "确认下单"
MAX_PRICE_CANDIDATES_PER_PLAN = 8


class AppError(RuntimeError):
    """可直接展示给用户的应用错误。"""


class PickupMethod(str, Enum):
    SELF_PICKUP = "self_pickup"
    DELIVERY = "delivery"
    DRIVE_THROUGH = "drive_through"


PICKUP_LABELS = {
    PickupMethod.SELF_PICKUP: "到店自取",
    PickupMethod.DELIVERY: "外送",
    PickupMethod.DRIVE_THROUGH: "得来速",
}


@dataclass(frozen=True)
class Store:
    code: str
    be_code: str
    name: str
    address: str
    open_minutes: int
    close_minutes: int
    business_status: bool = True
    distance_text: str = ""
    distance_km: float | None = None
    delivery_fee_cents: int = 0
    service_fee_cents: int = 0
    assistance: tuple[str, ...] = ()

    @property
    def is_24_hours(self) -> bool:
        return self.open_minutes == self.close_minutes


@dataclass(frozen=True)
class MenuItem:
    code: str
    name: str
    category: str
    price_cents: int
    servings: int = 1
    stock: int = 999  # 演示库存；真实菜单仅表示当前可售，不代表库存数量。
    spicy: bool = False
    tags: tuple[str, ...] = ()
    with_order: dict[str, Any] | None = None


@dataclass(frozen=True)
class LineItem:
    item: MenuItem
    quantity: int

    @property
    def subtotal_cents(self) -> int:
        return self.item.price_cents * self.quantity


@dataclass(frozen=True)
class Coupon:
    id: str
    code: str
    title: str
    products: tuple[str, ...] = ()
    pickup_methods: tuple[PickupMethod, ...] = ()
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    discount_cents: int = 0  # 仅演示使用；真实优惠金额必须由 calculate-price 返回。
    min_spend_cents: int = 0
    stackable: bool = False


@dataclass(frozen=True)
class Quote:
    line_items: tuple[LineItem, ...]
    assignments: tuple[tuple[int, Coupon], ...]
    merchandise_cents: int
    delivery_fee_cents: int
    service_fee_cents: int
    discount_cents: int
    total_cents: int
    verified: bool
    note: str
    raw: dict[str, Any]

    @property
    def coupons(self) -> tuple[Coupon, ...]:
        return tuple(coupon for _, coupon in self.assignments)


@dataclass(frozen=True)
class CandidatePlan:
    line_items: tuple[LineItem, ...]
    score: float
    reasons: tuple[str, ...]


@dataclass
class Order:
    id: str
    store_code: str
    pickup_method: PickupMethod
    quote: Quote
    status: str
    created_at: datetime | None
    payment_url: str | None
    is_demo: bool
    pay_id: str | None = None


@dataclass(frozen=True)
class PaymentRecord:
    order_id: str
    transaction_id: str
    amount_cents: int
    paid_at: datetime | None
    refund_cents: int = 0
    status: str = "PAID"


@dataclass(frozen=True)
class Participant:
    name: str
    weight: int = 1


def configured_token() -> str:
    return os.getenv("MCD_MCP_TOKEN", MCP_TOKEN).strip()


def require_token() -> str:
    token = configured_token()
    if not token or token == "YOUR_MCP_TOKEN":
        raise AppError(
            "尚未配置真实 MCP Token。请修改文件顶部 YOUR_MCP_TOKEN，"
            "或设置环境变量 MCD_MCP_TOKEN；真实 Token 不要公开发送。"
        )
    return token


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    return f"{sign}¥{value // 100}.{value % 100:02d}"


def yuan_to_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"非法元金额：{value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"非法元金额：{value!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"金额最多保留两位小数：{value!r}")
    return int(cents)


def parse_budget(text: str) -> int | None:
    if not text.strip():
        return None
    value = yuan_to_cents(text)
    if value < 0:
        raise ValueError("预算不能为负数")
    return value


def parse_minutes(value: Any) -> int:
    if isinstance(value, int) and 0 <= value <= 1440:
        return value
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"非法营业时间：{value}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 24 and 0 <= minute <= 59):
        raise ValueError(f"非法营业时间：{value}")
    return hour * 60 + minute


def is_open_at(store: Store, target: datetime) -> bool:
    if not store.business_status:
        return False
    if store.is_24_hours:
        return True
    minute = target.hour * 60 + target.minute
    if store.open_minutes < store.close_minutes:
        return store.open_minutes <= minute < store.close_minutes
    return minute >= store.open_minutes or minute < store.close_minutes


def business_hours(store: Store) -> str:
    if store.is_24_hours:
        return "24 小时营业"
    return (
        f"{store.open_minutes // 60:02d}:{store.open_minutes % 60:02d}–"
        f"{store.close_minutes // 60:02d}:{store.close_minutes % 60:02d}"
    )


def split_cents(total_cents: int, participants: list[Participant]) -> list[dict[str, Any]]:
    """最大余数法；以整数分保证分摊合计与账单完全一致。"""
    if not isinstance(total_cents, int) or total_cents < 0:
        raise ValueError("分摊总额必须是非负整数分")
    if not participants:
        raise ValueError("至少需要一位分摊成员")
    names = [participant.name.strip() for participant in participants]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("成员姓名不能为空或重复")
    weights = [participant.weight for participant in participants]
    if any(isinstance(weight, bool) or not isinstance(weight, int) or weight < 0 for weight in weights):
        raise ValueError("权重必须是非负整数")
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("权重总和必须大于 0")
    products = [total_cents * weight for weight in weights]
    amounts = [product // weight_sum for product in products]
    remainders = [product % weight_sum for product in products]
    remainder = total_cents - sum(amounts)
    order = sorted(range(len(weights)), key=lambda index: (-remainders[index], index))
    for index in order[:remainder]:
        amounts[index] += 1
    assert sum(amounts) == total_cents and all(amount >= 0 for amount in amounts)
    return [
        {"name": participant.name, "weight": participant.weight, "amount_cents": amount}
        for participant, amount in zip(participants, amounts)
    ]


def coupon_available(
    coupon: Coupon,
    lines: tuple[LineItem, ...],
    pickup: PickupMethod,
    target: datetime,
) -> bool:
    if coupon.pickup_methods and pickup not in coupon.pickup_methods:
        return False
    if coupon.starts_at and target < coupon.starts_at:
        return False
    if coupon.expires_at and target > coupon.expires_at:
        return False
    qualifying = sum(
        line.subtotal_cents for line in lines
        if not coupon.products or line.item.code in coupon.products
    )
    return qualifying >= coupon.min_spend_cents


def coupon_assignments(
    lines: tuple[LineItem, ...],
    coupons: tuple[Coupon, ...],
    pickup: PickupMethod,
    target: datetime,
    max_candidates: int = MAX_PRICE_CANDIDATES_PER_PLAN,
) -> list[tuple[tuple[int, Coupon], ...]]:
    """每个餐品行最多关联一张券；真实候选由 MCP 算价，不声称全局最优。"""
    eligible = [
        coupon for coupon in coupons
        if coupon_available(coupon, lines, pickup, target)
        and any(not coupon.products or line.item.code in coupon.products for line in lines)
    ]
    eligible.sort(key=lambda coupon: coupon.id)
    results: list[tuple[tuple[int, Coupon], ...]] = [()]
    for size in (1, 2):
        for combo in itertools.combinations(eligible, size):
            if size > 1 and any(not coupon.stackable for coupon in combo):
                continue
            options = [
                [index for index, line in enumerate(lines) if not coupon.products or line.item.code in coupon.products]
                for coupon in combo
            ]
            for indexes in itertools.product(*options):
                if len(set(indexes)) != len(indexes):
                    continue
                results.append(tuple(sorted(zip(indexes, combo), key=lambda pair: pair[0])))
                if len(results) >= max_candidates:
                    return results
    return results


def items_payload(
    lines: tuple[LineItem, ...], assignments: tuple[tuple[int, Coupon], ...]
) -> list[dict[str, Any]]:
    by_index = {index: coupon for index, coupon in assignments}
    items = []
    for index, line in enumerate(lines):
        item = {"productCode": line.item.code, "quantity": line.quantity}
        coupon = by_index.get(index)
        if coupon:
            if coupon.id:
                item["couponId"] = coupon.id
            if coupon.code:
                item["couponCode"] = coupon.code
        items.append(item)
    return items


def local_quote(
    lines: tuple[LineItem, ...],
    assignments: tuple[tuple[int, Coupon], ...],
    store: Store,
    pickup: PickupMethod,
) -> Quote:
    """只用于演示；真实价格必须调用 calculate-price。"""
    merchandise = sum(line.subtotal_cents for line in lines)
    delivery = store.delivery_fee_cents if pickup == PickupMethod.DELIVERY else 0
    discount = 0
    for _, coupon in assignments:
        qualifying = sum(
            line.subtotal_cents for line in lines
            if not coupon.products or line.item.code in coupon.products
        )
        discount += min(coupon.discount_cents, qualifying)
    discount = min(discount, merchandise + delivery + store.service_fee_cents)
    return Quote(
        lines, assignments, merchandise, delivery, store.service_fee_cents, discount,
        merchandise + delivery + store.service_fee_cents - discount,
        True, "演示本地价格引擎；真实项目以 MCP calculate-price 为准", {},
    )


def _main_options(menu: tuple[MenuItem, ...], people: int, flavor: str) -> list[tuple[LineItem, ...]]:
    mains = [item for item in menu if item.category == "main" and item.stock > 0]
    if flavor == "non_spicy":
        mains = [item for item in mains if not item.spicy]
    options: list[tuple[LineItem, ...]] = []
    for item in mains:
        if item.stock * item.servings >= people:
            options.append((LineItem(item, math.ceil(people / item.servings)),))
    if flavor == "mixed":
        spicy = [item for item in mains if item.spicy]
        mild = [item for item in mains if not item.spicy]
        for hot in spicy:
            for cool in mild:
                hot_qty = math.ceil((people // 2) / hot.servings)
                cool_qty = math.ceil((people - people // 2) / cool.servings)
                if hot_qty <= hot.stock and cool_qty <= cool.stock:
                    options.append((LineItem(hot, hot_qty), LineItem(cool, cool_qty)))
    return options


def _category_options(menu: tuple[MenuItem, ...], category: str, people: int) -> list[tuple[LineItem, ...]]:
    items = [item for item in menu if item.category == category and item.stock > 0]
    options: list[tuple[LineItem, ...]] = []
    for item in items:
        target = people if category == "drink" else max(1, math.ceil(people / 2))
        quantity = math.ceil(target / item.servings)
        if quantity <= item.stock:
            options.append((LineItem(item, quantity),))
    return options or [()]


def recommend_plans(
    menu: tuple[MenuItem, ...], people: int, flavor: str, max_plans: int = 3
) -> list[CandidatePlan]:
    """先用菜单标价形成少量候选，再用真实算价或演示优惠引擎排序。"""
    if people <= 0:
        raise ValueError("人数必须大于 0")
    mains = _main_options(menu, people, flavor)
    if not mains:
        raise ValueError("当前菜单无法形成满足人数与口味要求的主食方案")
    snacks = _category_options(menu, "snack", people)
    drinks = _category_options(menu, "drink", people)
    seen: set[tuple[tuple[str, int], ...]] = set()
    plans: list[CandidatePlan] = []
    for main in mains:
        for snack in snacks:
            for drink in drinks:
                lines = tuple(main + snack + drink)
                signature = tuple(sorted((line.item.code, line.quantity) for line in lines))
                if signature in seen:
                    continue
                seen.add(signature)
                subtotal = sum(line.subtotal_cents for line in lines)
                spicy_count = sum(line.quantity for line in main if line.item.spicy)
                penalty = 10000 if flavor == "mixed" and (not spicy_count or spicy_count == people) else 0
                reasons = [f"主食覆盖 {people} 人", f"餐品标价合计 {money(subtotal)}"]
                if flavor == "non_spicy":
                    reasons.append("仅按名称和标签排除辣味，忌口仍需核验餐品详情")
                plans.append(CandidatePlan(lines, subtotal / 100 + penalty - len(lines), tuple(reasons)))
    plans.sort(key=lambda plan: (plan.score, sum(line.subtotal_cents for line in plan.line_items)))
    return plans[:max_plans]


def estimated_bill(quote: Quote) -> dict[str, Any]:
    return {
        "type": "预估账单", "status": "已核验" if quote.verified else "待确认",
        "merchandise_cents": quote.merchandise_cents,
        "delivery_fee_cents": quote.delivery_fee_cents,
        "service_fee_cents": quote.service_fee_cents,
        "discount_cents": quote.discount_cents,
        "estimated_total_cents": quote.total_cents, "note": quote.note,
    }


def actual_bill(order: Order, payment: PaymentRecord | None) -> dict[str, Any]:
    if payment is None:
        return {
            "type": "实付账单", "order_id": order.id, "status": "待确认",
            "order_due_cents": order.quote.total_cents, "paid_cents": None,
            "refund_cents": None, "net_paid_cents": None,
            "note": "订单创建不等于支付成功；取得真实支付结果前不得报销。",
        }
    if payment.order_id != order.id or payment.status != "PAID":
        raise ValueError("支付记录与订单不一致或尚未支付成功")
    if not 0 <= payment.refund_cents <= payment.amount_cents:
        raise ValueError("退款金额非法")
    return {
        "type": "实付账单", "order_id": order.id,
        "status": "已核验（演示凭证）" if order.is_demo else "依据真实订单状态核验",
        "paid_cents": payment.amount_cents, "refund_cents": payment.refund_cents,
        "net_paid_cents": payment.amount_cents - payment.refund_cents,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "transaction_id": payment.transaction_id,
    }


def three_bills(order: Order, payment: PaymentRecord | None, participants: list[Participant]) -> dict[str, Any]:
    paid = payment is not None and payment.status == "PAID"
    total = payment.amount_cents - payment.refund_cents if paid else order.quote.total_cents
    return {
        "estimated": estimated_bill(order.quote),
        "actual": actual_bill(order, payment),
        "split": {
            "type": "分摊账单", "status": "正式分摊" if paid else "预估分摊",
            "total_cents": total, "shares": split_cents(total, participants),
        },
    }


def invoice_guide(order: Order, payment: PaymentRecord | None) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "invoice_status": "待支付后办理" if payment is None else "请在官方入口核验可开票金额",
        "entry": "麦当劳官方订单或发票入口，以页面实际规则为准",
        "reimbursement_materials": ["订单明细", "实付账单", "支付凭证", "分摊表"],
        "note": "支付链接不是支付凭证；不虚构开票链接、税额或可开票金额。",
    }


def demo_data() -> tuple[tuple[Store, ...], tuple[MenuItem, ...], tuple[Coupon, ...], datetime]:
    target = datetime.fromisoformat("2026-10-24T23:30:00")
    stores = (
        Store("S001", "BE01", "科技园店（演示）", "示例市科技园 1 号", 0, 0,
              distance_text="0.8 km", distance_km=0.8, delivery_fee_cents=800,
              assistance=("专人分餐", "保鲜速达")),
        Store("S002", "BE02", "创业路店（演示）", "示例市创业路 8 号", 360, 1380,
              distance_text="1.6 km", distance_km=1.6, delivery_fee_cents=600),
        Store("S003", "BE03", "深夜中心店（演示）", "示例市夜宵街 18 号", 1080, 360,
              distance_text="2.8 km", distance_km=2.8, assistance=("专人分餐",)),
    )
    menu = (
        MenuItem("M001", "板烧鸡腿堡（演示）", "main", 1800, tags=("汉堡", "不辣")),
        MenuItem("M002", "麦辣鸡腿堡（演示）", "main", 1900, spicy=True, tags=("汉堡", "辣")),
        MenuItem("M003", "双层吉士汉堡（演示）", "main", 2200, tags=("汉堡", "牛肉")),
        MenuItem("S101", "鸡翅分享盒（演示）", "snack", 2900, servings=4, stock=20, spicy=True, tags=("分享", "辣")),
        MenuItem("S102", "麦乐鸡（演示）", "snack", 1500, servings=2, stock=30, tags=("分享",)),
        MenuItem("S103", "中薯条（演示）", "snack", 800, stock=50),
        MenuItem("D001", "可乐（演示）", "drink", 500, stock=80),
        MenuItem("D002", "无糖可乐（演示）", "drink", 600, stock=80, tags=("无糖",)),
    )
    both = (PickupMethod.SELF_PICKUP, PickupMethod.DELIVERY)
    expiry = datetime.fromisoformat("2026-12-31T23:59:59")
    coupons = (
        Coupon("C200", "", "满 200 减 30（演示）", pickup_methods=both,
               expires_at=expiry, discount_cents=3000, min_spend_cents=20000),
        Coupon("CSNACK", "", "小食满 25 减 8（演示）", products=("S101", "S102", "S103"),
               pickup_methods=both, expires_at=expiry, discount_cents=800,
               min_spend_cents=2500, stackable=True),
        Coupon("CDRINK", "", "饮品满 40 减 5（演示）", products=("D001", "D002"),
               pickup_methods=(PickupMethod.SELF_PICKUP,), expires_at=expiry,
               discount_cents=500, min_spend_cents=4000, stackable=True),
        Coupon("CDELIVERY", "", "外送满 199 减 25（演示）",
               pickup_methods=(PickupMethod.DELIVERY,), expires_at=expiry,
               discount_cents=2500, min_spend_cents=19900),
    )
    return stores, menu, coupons, target


# --------------------------- 真实 MCP 客户端与官方字段解析 ---------------------------


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def decode_content(payload: dict[str, Any]) -> Any:
    structured = payload.get("structuredContent", payload.get("structured_content"))
    if structured is not None:
        return structured
    texts = [
        str(block.get("text", "")) for block in payload.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    combined = "\n".join(text for text in texts if text).strip()
    if not combined:
        return payload
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return {"text": combined}


class MCPClient:
    """官方 MCP SDK Streamable HTTP 客户端；写操作异常绝不自动重放。"""

    def __init__(self, url: str = MCP_URL, timeout: int = 60):
        self.url = url
        self.timeout = timeout
        self._tools: list[dict[str, Any]] | None = None

    @staticmethod
    def _transport():
        try:
            from mcp.client.streamable_http import streamable_http_client
            return streamable_http_client
        except ImportError:
            try:
                from mcp.client.streamable_http import streamablehttp_client
                return streamablehttp_client
            except ImportError as exc:
                raise AppError('缺少 MCP SDK，请执行：python -m pip install "mcp>=1.10,<2.0"') from exc

    async def _with_session(self, operation):
        try:
            from mcp import ClientSession
        except ImportError as exc:
            raise AppError('缺少 MCP SDK，请执行：python -m pip install "mcp>=1.10,<2.0"') from exc
        headers = {"Authorization": f"Bearer {require_token()}"}
        transport = self._transport()
        async with transport(self.url, headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await operation(session)

    def _run(self, operation):
        try:
            return asyncio.run(asyncio.wait_for(self._with_session(operation), self.timeout))
        except TimeoutError as exc:
            raise AppError("MCP 请求超时；不会自动重试。请先检查订单列表或服务端结果。") from exc
        except AppError:
            raise
        except Exception as exc:
            raise AppError(f"MCP 调用失败：{type(exc).__name__}: {exc}") from exc

    def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is not None:
            return self._tools

        async def operation(session):
            result = await session.list_tools()
            return _plain(result.tools)

        self._tools = self._run(operation)
        return self._tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        async def operation(session):
            return await session.call_tool(name, arguments or {})

        result = _plain(self._run(operation))
        if result.get("isError") or result.get("is_error"):
            raise AppError(f"工具 {name} 返回错误：{json.dumps(result, ensure_ascii=False)}")
        return result

    def api(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        decoded = decode_content(self.call_tool(name, arguments))
        if isinstance(decoded, dict) and "success" in decoded:
            if not decoded.get("success") or decoded.get("code") not in (200, "200", None):
                raise AppError(f"工具 {name} 返回业务错误：{json.dumps(decoded, ensure_ascii=False)}")
            return decoded.get("data")
        return decoded


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "是"}


def parse_store(data: dict[str, Any]) -> Store:
    distance_text = str(data.get("distanceText", data.get("distance", "")) or "")
    distance_km = None
    match = re.fullmatch(r"\s*([\d.]+)\s*(km|m)\s*", distance_text, re.IGNORECASE)
    if match:
        value = float(match.group(1))
        distance_km = value if match.group(2).lower() == "km" else value / 1000
    return Store(
        code=str(data["storeCode"]), be_code=str(data.get("beCode", "")),
        name=str(data.get("storeName", data["storeCode"])), address=str(data.get("address", "")),
        open_minutes=parse_minutes(data["businessStartTime"]),
        close_minutes=parse_minutes(data["businessEndTime"]),
        business_status=_as_bool(data.get("businessStatus", True)),
        distance_text=distance_text, distance_km=distance_km,
    )


def _classify_meal(name: str, category: str) -> str:
    text = f"{name} {category}"
    if any(word in text for word in ("饮品", "饮料", "可乐", "咖啡", "雪碧", "果汁", "豆浆", "奶茶", "红茶")):
        return "drink"
    if any(word in text for word in ("小食", "甜品", "薯", "鸡翅", "麦乐鸡", "鸡块", "派", "冰淇淋")):
        return "snack"
    return "main"


def parse_menu(data: dict[str, Any]) -> tuple[MenuItem, ...]:
    category_by_code: dict[str, str] = {}
    for category in data.get("categories", []):
        for meal in category.get("meals", []):
            category_by_code[str(meal.get("code"))] = str(category.get("name", ""))
    result = []
    for code, meal in data.get("meals", {}).items():
        name = str(meal.get("name", code))
        category = category_by_code.get(str(code), "")
        tags = tuple(str(tag) for tag in meal.get("tags", []))
        spicy = "辣" in name or any("辣" in tag for tag in tags)
        result.append(MenuItem(
            code=str(code), name=name,
            category=_classify_meal(name, category),
            price_cents=yuan_to_cents(meal.get("currentPrice", meal.get("originalPrice"))),
            tags=tags, spicy=spicy, with_order=meal.get("withOrder"),
        ))
    if not result:
        raise AppError("当前门店没有返回可售餐品")
    return tuple(result)


def parse_coupons(data: Any) -> tuple[Coupon, ...]:
    if not isinstance(data, list):
        return ()
    coupons = []
    for item in data:
        start = end = None
        trade_time = str(item.get("tradeDateTime", ""))
        match = re.search(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*-\s*"
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", trade_time,
        )
        if match:
            start = datetime.fromisoformat(match.group(1))
            end = datetime.fromisoformat(match.group(2))
        coupons.append(Coupon(
            id=str(item.get("couponId", "")), code=str(item.get("couponCode", "")),
            title=str(item.get("title", item.get("couponName", "优惠券"))),
            products=tuple(str(product.get("productCode")) for product in item.get("products", [])),
            starts_at=start, expires_at=end,
            # 文档未提供门槛、互斥或优惠金额；真实候选均交给 MCP 验证。
            stackable=True,
        ))
    return tuple(coupons)


def parse_price(
    data: dict[str, Any], lines: tuple[LineItem, ...],
    assignments: tuple[tuple[int, Coupon], ...],
) -> Quote:
    return Quote(
        line_items=lines, assignments=assignments,
        merchandise_cents=int(data.get("productPrice", 0)),
        delivery_fee_cents=int(data.get("deliveryPrice", 0)),
        service_fee_cents=int(data.get("tablewarePrice", 0)),
        discount_cents=int(data.get("discount", 0)), total_cents=int(data["price"]),
        verified=True, note="MCP calculate-price 已核验；金额单位为分", raw=data,
    )


def _optional_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def parse_created_order(data: dict[str, Any], store: Store, pickup: PickupMethod, quote: Quote) -> Order:
    detail = data.get("orderDetail", {})
    total = yuan_to_cents(detail.get("realTotalAmount", Decimal(quote.total_cents) / 100))
    actual_quote = Quote(
        quote.line_items, quote.assignments,
        yuan_to_cents(detail.get("productPrice", Decimal(quote.merchandise_cents) / 100)),
        yuan_to_cents(detail.get("realDeliveryPrice", Decimal(quote.delivery_fee_cents) / 100)),
        quote.service_fee_cents,
        yuan_to_cents(detail.get("totalDiscountAmount", Decimal(quote.discount_cents) / 100)),
        total, True, "create-order 返回订单金额", quote.raw,
    )
    return Order(
        id=str(data["orderId"]), store_code=store.code, pickup_method=pickup,
        quote=actual_quote, status=str(detail.get("orderStatus", "UNKNOWN")),
        created_at=_optional_datetime(detail.get("createTime")),
        payment_url=data.get("payH5Url"), is_demo=False, pay_id=data.get("payId"),
    )


def order_from_detail(data: dict[str, Any]) -> Order:
    total = yuan_to_cents(data.get("realTotalAmount", data.get("totalAmount", "0")))
    merchandise = yuan_to_cents(data.get("productPrice", "0"))
    delivery = yuan_to_cents(data.get("realDeliveryPrice", data.get("deliveryPrice", "0")))
    discount = yuan_to_cents(data.get("totalDiscountAmount", "0"))
    quote = Quote((), (), merchandise, delivery, 0, discount, total, True, "MCP query-order 返回", data)
    return Order(
        id=str(data["orderId"]), store_code="", pickup_method=PickupMethod.SELF_PICKUP,
        quote=quote, status=str(data.get("orderStatus", "UNKNOWN")),
        created_at=_optional_datetime(data.get("createTime")), payment_url=None, is_demo=False,
    )


def order_fingerprint(store: Store, pickup: PickupMethod, quote: Quote) -> str:
    payload = {
        "store": store.code, "pickup": pickup.value,
        "items": items_payload(quote.line_items, quote.assignments), "total": quote.total_cents,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class OrderGuard:
    def __init__(self):
        self._submitted: dict[str, str] = {}

    def before_create(self, store: Store, pickup: PickupMethod, quote: Quote) -> str:
        fingerprint = order_fingerprint(store, pickup, quote)
        if fingerprint in self._submitted:
            raise AppError(f"相同订单已提交：{self._submitted[fingerprint]}，请勿重复创建")
        return fingerprint

    def after_create(self, fingerprint: str, order: Order) -> None:
        self._submitted[fingerprint] = order.id


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def save_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def print_stores(stores: list[Store]) -> None:
    for index, store in enumerate(stores, 1):
        distance = f"｜{store.distance_text}" if store.distance_text else ""
        print(f"  {index}. {store.name}｜{business_hours(store)}{distance}｜{store.address}")


def print_quote(quote: Quote) -> None:
    for line in quote.line_items:
        print(f"  - {line.item.name} × {line.quantity}：{money(line.subtotal_cents)}")
    print("优惠券：" + ("、".join(coupon.title for coupon in quote.coupons) if quote.coupons else "未使用"))
    print(f"餐品：{money(quote.merchandise_cents)}｜配送：{money(quote.delivery_fee_cents)}"
          f"｜餐具/服务：{money(quote.service_fee_cents)}｜优惠：-{money(quote.discount_cents)}")
    print(f"合计：{money(quote.total_cents)}｜{quote.note}")


def print_bills(bills: dict[str, Any]) -> None:
    estimate = bills["estimated"]
    print("\n===== 三张账单 =====")
    print(f"预估应付：{money(estimate['estimated_total_cents'])}（{estimate['status']}）")
    actual = bills["actual"]
    if actual["paid_cents"] is None:
        print(f"实付账单：待确认｜订单应付：{money(actual['order_due_cents'])}")
    else:
        print(f"实付账单：{money(actual['paid_cents'])}｜净实付：{money(actual['net_paid_cents'])}")
    split = bills["split"]
    print(f"分摊账单（{split['status']}）：")
    for share in split["shares"]:
        print(f"  - {share['name']}：{money(share['amount_cents'])}")
    print(f"分摊合计：{money(split['total_cents'])}")


def choose(items: list[Any], label: str = "方案") -> Any:
    if len(items) == 1:
        return items[0]
    raw = input(f"请选择{label} [1-{len(items)}，默认 1]：").strip() or "1"
    index = int(raw)
    if not 1 <= index <= len(items):
        raise ValueError(f"{label}编号超出范围")
    return items[index - 1]


def collect_participants(people: int, names: str | None = None) -> list[Participant]:
    if names is None:
        names = input("分摊成员（逗号分隔；回车按人数生成）：").strip()
    values = [name.strip() for name in re.split(r"[,，]", names) if name.strip()]
    if not values:
        values = [f"成员{index}" for index in range(1, people + 1)]
    return [Participant(name) for name in values]


def take_way_code(quote: Quote) -> str:
    options = quote.raw.get("takeWayList", [])
    if isinstance(options, dict):
        options = [options]
    if not options:
        raise AppError("到店下单需要 takeWayCode，但本次算价未返回 takeWayList，无法继续创建订单")
    normalized = []
    for option in options:
        if isinstance(option, dict):
            code = option.get("takeWayCode", option.get("code", option.get("takeWay", option.get("value"))))
            name = option.get("takeWayName", option.get("name", option.get("label", code)))
        else:
            code, name = option, option
        if code:
            normalized.append((str(code), str(name)))
    if not normalized:
        raise AppError("takeWayList 中未识别到取餐码，请用 list-tools/call-tool 核验实际结构")
    for index, (_, name) in enumerate(normalized, 1):
        print(f"  {index}. {name}")
    return choose(normalized, "取餐方式")[0]


def select_assistance(data: dict[str, Any]) -> str:
    items = [item for item in data.get("mealAssistanceItems", []) if _as_bool(item.get("enable", True))]
    if not items:
        raise AppError("企业团餐需要助餐服务码，但门店未返回可用服务")
    for promotion in data.get("promotions", []):
        print("助餐优惠：" + str(promotion))
    normalized = []
    for item in items:
        name = item.get("gmServiceName", item.get("gmServiceCode"))
        services = "、".join(item.get("serviceItems", []))
        normalized.append((str(item["gmServiceCode"]), f"{name}（{services}）"))
    for index, (_, label) in enumerate(normalized, 1):
        print(f"  {index}. {label}")
    return choose(normalized, "助餐服务")[0]


def select_delivery_address(client: MCPClient, city: str) -> str:
    data = client.api("delivery-query-addresses", {}) or {}
    addresses = data.get("addresses", []) if isinstance(data, dict) else []
    if not addresses:
        answer = input("没有可用外送地址，是否现在新增？[y/N]：").strip().lower()
        if answer not in {"y", "yes"}:
            raise AppError("外送下单需要收货地址")
        contact = input("联系人：").strip()
        gender = input("性别（先生/女士，可留空）：").strip()
        phone = input("11 位手机号：").strip()
        address = input("配送地址：").strip()
        detail = input("门牌号：").strip()
        arguments = {
            "city": input(f"城市 [{city}]：").strip() or city,
            "contactName": contact, "phone": phone,
            "address": address, "addressDetail": detail,
        }
        if gender:
            arguments["gender"] = gender
        created = client.api("delivery-create-address", arguments)
        return str(created["addressId"])
    for index, address in enumerate(addresses, 1):
        print(f"  {index}. {address.get('fullAddress', '')}｜{address.get('contactName', '')}｜{address.get('phone', '')}")
    return str(choose(addresses, "收货地址")["addressId"])


def run_real() -> dict[str, Any]:
    require_token()
    client = MCPClient()
    now_data = client.api("now-time-info", {})
    now = datetime.fromisoformat(now_data["datetime"])
    print("【真实 MCP 模式】所有门店、餐品、优惠、价格和订单均来自真实服务。")
    people = int(input("用餐人数：").strip())
    budget = parse_budget(input("总预算（元，回车不限）："))
    mode_key = input("取餐方式：1 到店自取，2 外送，3 得来速 [默认 1]：").strip() or "1"
    modes = {"1": PickupMethod.SELF_PICKUP, "2": PickupMethod.DELIVERY, "3": PickupMethod.DRIVE_THROUGH}
    if mode_key not in modes:
        raise ValueError("取餐方式必须选择 1、2 或 3")
    pickup = modes[mode_key]
    flavor_key = input("口味：1 混合，2 不辣，3 辣 [默认 1]：").strip() or "1"
    flavors = {"1": "mixed", "2": "non_spicy", "3": "spicy"}
    if flavor_key not in flavors:
        raise ValueError("口味必须选择 1、2 或 3")
    flavor = flavors[flavor_key]
    time_text = input(f"预约时间（yyyy-MM-dd HH:mm；回车立即用餐，当前 {now:%Y-%m-%d %H:%M}）：").strip()
    target = datetime.strptime(time_text, "%Y-%m-%d %H:%M") if time_text else now
    reservation = target.strftime("%Y-%m-%d %H:%M") if time_text else None
    city = input("城市（如南京市）：").strip()
    address_id = None
    enterprise = False
    if pickup == PickupMethod.DELIVERY:
        address_id = select_delivery_address(client, city)
        enterprise_answer = input(f"是否企业团餐？[{'Y/n' if people >= 10 else 'y/N'}]：").strip().lower()
        if people >= 10:
            enterprise = enterprise_answer not in {"n", "no"}
        else:
            enterprise = enterprise_answer in {"y", "yes"}
    if pickup == PickupMethod.SELF_PICKUP:
        be_type = 1
    elif pickup == PickupMethod.DRIVE_THROUGH:
        be_type = 5
    else:
        be_type = 6 if enterprise else 2

    if pickup == PickupMethod.DELIVERY:
        stores_data = client.api("delivery-query-stores", {"addressId": address_id, "beType": be_type})
    else:
        keyword = input("位置关键词（商圈、地标或地址）：").strip()
        if not city or not keyword:
            raise ValueError("到店查询需要城市和位置关键词")
        stores_data = client.api("query-nearby-stores", {
            "searchType": 2, "beType": be_type, "city": city, "keyword": keyword,
        })
    stores = [parse_store(item) for item in (stores_data or [])]
    stores = [store for store in stores if is_open_at(store, target)]
    stores.sort(key=lambda store: (store.distance_km is None, store.distance_km or 0, store.code))
    if not stores:
        raise AppError("目标时间没有营业且支持该方式的门店；请调整地址、时间或取餐方式")
    print("\n真实营业门店：")
    print_stores(stores[:3])
    store = choose(stores[:3], "门店")
    order_type = 2 if pickup == PickupMethod.DELIVERY else 1
    base = {"storeCode": store.code, "beCode": store.be_code, "orderType": order_type, "beType": be_type}
    if reservation:
        base["reservationDate"] = reservation
    gm_service_code = None
    if enterprise:
        assistance_data = client.api("query-meal-assistance", {
            "storeCode": store.code, "beCode": store.be_code, "beType": 6, "orderType": 2,
            **({"reservationDate": reservation} if reservation else {}),
        })
        gm_service_code = select_assistance(assistance_data)
    need_tableware = input("需要餐具？[Y/n]：").strip().lower() not in {"n", "no"}
    remark = ""
    if pickup == PickupMethod.DELIVERY:
        remark = input("订单备注（外送可选，最多 50 字）：").strip()
        if len(remark) > 50:
            raise ValueError("订单备注不能超过 50 字")

    menu = parse_menu(client.api("query-meals", base))
    initial_coupons = parse_coupons(client.api("query-store-coupons", base))
    client.api("auto-bind-coupons", {})
    client.api("query-my-coupons", {})
    coupons = parse_coupons(client.api("query-store-coupons", base))
    print(f"领券流程完成：初次查询 {len(initial_coupons)} 张，当前门店可用券 {len(coupons)} 张")
    plans = recommend_plans(menu, people, flavor, max_plans=3)
    price_rows: list[tuple[CandidatePlan, Quote]] = []
    errors = []
    for plan in plans:
        assignments_list = coupon_assignments(plan.line_items, coupons, pickup, target)
        for assignments in assignments_list:
            arguments = dict(base) | {
                "items": items_payload(plan.line_items, assignments),
                "needTableware": need_tableware,
            }
            if gm_service_code:
                arguments["gmServiceCode"] = gm_service_code
            try:
                price_data = client.api("calculate-price", arguments)
                price_rows.append((plan, parse_price(price_data, plan.line_items, assignments)))
            except AppError as exc:
                if "429" in str(exc):
                    raise
                errors.append(str(exc))
    if not price_rows:
        raise AppError("所有候选均无法完成真实算价：" + "；".join(errors[:3]))
    price_rows = dedupe_price_rows(price_rows)
    within = [row for row in price_rows if budget is None or row[1].total_cents <= budget]
    if budget is not None and not within:
        print("警告：已试算候选均未满足预算，以下展示最低的已核验价格。")
    ranked = within or price_rows
    ranked.sort(key=lambda row: (row[1].total_cents, row[0].score))
    print("\n已验证候选中的最优方案（不是全局穷举承诺）：")
    for index, (plan, quote) in enumerate(ranked[:3], 1):
        print(f"\n方案 {index}：{'；'.join(plan.reasons)}")
        print_quote(quote)
    plan, quote = choose(ranked[:3], "方案")
    if budget is not None and quote.total_cents > budget:
        print(f"警告：方案超出预算 {money(quote.total_cents - budget)}")
    selected_take_way = take_way_code(quote) if pickup != PickupMethod.DELIVERY else None

    print("\n待确认真实订单：")
    print(f"门店：{store.name}｜取餐：{PICKUP_LABELS[pickup]}｜预约：{reservation or '立即'}")
    print_quote(quote)
    if input(f"请输入“{CONFIRM_TEXT}”创建真实订单：").strip() != CONFIRM_TEXT:
        raise AppError("用户未确认，未创建订单")
    guard = OrderGuard()
    fingerprint = guard.before_create(store, pickup, quote)
    order_arguments = {
        "storeCode": store.code, "beCode": store.be_code, "beType": be_type,
        "items": items_payload(quote.line_items, quote.assignments),
        "needTableware": need_tableware,
    }
    if reservation:
        order_arguments["reservationDate"] = reservation
    if gm_service_code:
        order_arguments["gmServiceCode"] = gm_service_code
    if address_id:
        order_arguments["addressId"] = address_id
    if selected_take_way:
        order_arguments["takeWayCode"] = selected_take_way
    if remark:
        order_arguments["remark"] = remark
    order = parse_created_order(client.api("create-order", order_arguments), store, pickup, quote)
    guard.after_create(fingerprint, order)
    print(f"\n真实订单已创建：{order.id}｜状态：{order.status}")
    if order.payment_url:
        print("支付链接：" + order.payment_url)
    else:
        print("服务未返回支付链接，请在官方订单页面核实。")
    print("订单创建不等于支付成功；实付状态仍为待确认。")
    participants = collect_participants(people)
    bills = three_bills(order, None, participants)
    print_bills(bills)
    guide = invoice_guide(order, None)
    result = {
        "mode": "real_mcp", "store": store, "quote": quote, "order": order,
        "bills": bills, "invoice_guide": guide, "payment": None,
    }
    path = Path.cwd() / f"night_group_meal_real_{re.sub(r'[^0-9A-Za-z_-]', '_', order.id)}.json"
    save_result(path, result)
    print(f"结果已保存：{path}")
    print(f"支付后可运行：python {Path(__file__).name} settle {order.id} --names " + ",".join(p.name for p in participants))
    return result


PAID_ORDER_STATUSES = {"已支付", "待取餐", "制作中", "已完成", "配送中", "已送达", "餐厅已接单"}


def run_settle(order_id: str, names: str | None) -> dict[str, Any]:
    require_token()
    data = MCPClient().api("query-order", {"orderId": order_id})
    order = order_from_detail(data)
    participants = collect_participants(1, names)
    payment = None
    if order.status in PAID_ORDER_STATUSES:
        payment = PaymentRecord(order.id, f"MCP-ORDER-{order.id}", order.quote.total_cents, None)
    bills = three_bills(order, payment, participants)
    print(f"真实订单状态：{order.status}")
    if payment is None:
        print("当前状态不能核验为已支付，保留实付待确认。")
    print_bills(bills)
    result = {"mode": "real_mcp_settlement", "order": order, "bills": bills, "invoice_guide": invoice_guide(order, payment)}
    path = Path.cwd() / f"night_group_meal_settlement_{re.sub(r'[^0-9A-Za-z_-]', '_', order.id)}.json"
    save_result(path, result)
    print(f"结算结果已保存：{path}")
    return result


# --------------------------- 独立演示、内置测试与命令行入口 ---------------------------


def dedupe_price_rows(rows: list[tuple[CandidatePlan, Quote]]) -> list[tuple[CandidatePlan, Quote]]:
    """同一餐品组合、同一券组合和同一价格只展示一次。"""
    unique = {}
    for plan, quote in rows:
        items = tuple(sorted((line.item.code, line.quantity) for line in quote.line_items))
        coupons = tuple(sorted((coupon.id, coupon.code) for coupon in quote.coupons))
        unique.setdefault((items, coupons, quote.total_cents), (plan, quote))
    return list(unique.values())


def demo_ranked_plans(
    menu: tuple[MenuItem, ...], coupons: tuple[Coupon, ...], store: Store,
    people: int, flavor: str, target: datetime, budget: int | None,
) -> list[tuple[CandidatePlan, Quote]]:
    rows: list[tuple[CandidatePlan, Quote]] = []
    for plan in recommend_plans(menu, people, flavor):
        for assignments in coupon_assignments(plan.line_items, coupons, PickupMethod.SELF_PICKUP, target):
            rows.append((plan, local_quote(plan.line_items, assignments, store, PickupMethod.SELF_PICKUP)))
    rows = dedupe_price_rows(rows)
    within = [row for row in rows if budget is None or row[1].total_cents <= budget]
    ranked = within or rows
    ranked.sort(key=lambda row: (row[1].total_cents, row[0].score))
    return ranked


def run_demo(assume_yes: bool = False, output: Path | None = None) -> dict[str, Any]:
    print("【演示模式】所有门店、餐品、订单和支付凭证均为示例，不访问真实麦当劳服务。")
    stores, menu, coupons, target = demo_data()
    people, budget, flavor = 8, 20000, "mixed"
    pickup = PickupMethod.SELF_PICKUP
    available_stores = [store for store in stores if is_open_at(store, target) and (store.distance_km or 99) <= 5]
    available_stores.sort(key=lambda store: store.distance_km or 99)
    if not available_stores:
        raise AppError("演示场景无可用门店")
    print_stores(available_stores)
    store = available_stores[0]
    print("演示领券：成功 3 张，失败 1 张；失败券未计入优惠。")
    ranked = demo_ranked_plans(menu, coupons, store, people, flavor, target, budget)
    for index, (plan, quote) in enumerate(ranked[:2], 1):
        print(f"\n方案 {index}：{'；'.join(plan.reasons)}")
        print_quote(quote)
    plan, quote = (ranked[0] if assume_yes else choose(ranked[:2], "方案"))
    confirmation = CONFIRM_TEXT if assume_yes else input(f"请输入“{CONFIRM_TEXT}”创建演示订单：")
    if confirmation.strip() != CONFIRM_TEXT:
        raise AppError("未确认，未创建演示订单")
    guard = OrderGuard()
    fingerprint = guard.before_create(store, pickup, quote)
    order = Order(
        id="DEMO-" + fingerprint[:12], store_code=store.code, pickup_method=pickup,
        quote=quote, status="DEMO_PENDING_PAYMENT", created_at=datetime.now(),
        payment_url=None, is_demo=True,
    )
    guard.after_create(fingerprint, order)
    print(f"\n演示订单：{order.id}｜{order.status}｜无真实支付链接")
    use_payment = assume_yes or input("是否生成明确标注的演示支付凭证？[Y/n]：").strip().lower() not in {"n", "no"}
    payment = PaymentRecord(order.id, "DEMO-TRANSACTION-NOT-REAL", quote.total_cents, target) if use_payment else None
    participants = collect_participants(people, None if not assume_yes else "")
    bills = three_bills(order, payment, participants)
    print_bills(bills)
    guide = invoice_guide(order, payment)
    print("开票引导：" + guide["entry"])
    result = {
        "mode": "demo", "notice": "演示数据与演示凭证，不是真实麦当劳订单或支付记录。",
        "store": store, "plan": plan, "quote": quote, "order": order,
        "payment": payment, "bills": bills, "invoice_guide": guide,
    }
    path = output or Path.cwd() / "night_group_meal_demo_result.json"
    save_result(path, result)
    print(f"演示结果已保存：{path}")
    return result


class StoreAndSplitTests(unittest.TestCase):
    def test_cross_midnight_and_closed_store(self):
        stores, _, _, target = demo_data()
        self.assertTrue(is_open_at(stores[0], target))
        self.assertFalse(is_open_at(stores[1], target))
        self.assertTrue(is_open_at(stores[2], target))
        self.assertTrue(is_open_at(stores[2], datetime.fromisoformat("2026-10-25T05:59:59")))
        self.assertFalse(is_open_at(stores[2], datetime.fromisoformat("2026-10-25T06:00:00")))

    def test_equal_and_weighted_split(self):
        shares = split_cents(10000, [Participant("甲"), Participant("乙"), Participant("丙")])
        self.assertEqual([share["amount_cents"] for share in shares], [3334, 3333, 3333])
        weighted = split_cents(1000, [Participant("甲", 1), Participant("乙", 2), Participant("丙", 3)])
        self.assertEqual([share["amount_cents"] for share in weighted], [167, 333, 500])

    def test_many_split_amounts(self):
        for total in range(1001):
            for weights in ([1, 1, 1], [1, 2, 3, 4], [0, 3, 0, 2], [7]):
                participants = [Participant(str(index), weight) for index, weight in enumerate(weights)]
                self.assertEqual(sum(share["amount_cents"] for share in split_cents(total, participants)), total)

    def test_invalid_split(self):
        with self.assertRaises(ValueError):
            split_cents(-1, [Participant("甲")])
        with self.assertRaises(ValueError):
            split_cents(1, [Participant("甲", 0)])


class RecommendationAndBillingTests(unittest.TestCase):
    def test_demo_recommendation_meets_budget_and_flavor(self):
        stores, menu, coupons, target = demo_data()
        ranked = demo_ranked_plans(menu, coupons, stores[0], 8, "mixed", target, 20000)
        plan, quote = ranked[0]
        self.assertEqual(quote.total_cents, 18700)
        self.assertLessEqual(quote.total_cents, 20000)
        self.assertEqual({line.item.category for line in quote.line_items}, {"main", "snack", "drink"})
        self.assertTrue(any(line.item.spicy for line in quote.line_items if line.item.category == "main"))
        self.assertTrue(any(not line.item.spicy for line in quote.line_items if line.item.category == "main"))

    def test_non_spicy_recommendation(self):
        _, menu, _, _ = demo_data()
        plans = recommend_plans(menu, 8, "non_spicy")
        self.assertTrue(plans)
        self.assertTrue(all(not line.item.spicy for plan in plans for line in plan.line_items if line.item.category == "main"))

    def test_pending_and_demo_paid_bills(self):
        stores, menu, coupons, target = demo_data()
        _, quote = demo_ranked_plans(menu, coupons, stores[0], 8, "mixed", target, 20000)[0]
        order = Order("DEMO-1", stores[0].code, PickupMethod.SELF_PICKUP, quote, "DEMO_PENDING_PAYMENT", target, None, True)
        participants = [Participant(f"成员{index}") for index in range(8)]
        pending = three_bills(order, None, participants)
        self.assertEqual(pending["actual"]["status"], "待确认")
        payment = PaymentRecord(order.id, "DEMO", quote.total_cents, target)
        paid = three_bills(order, payment, participants)
        self.assertEqual(paid["split"]["status"], "正式分摊")
        self.assertEqual(sum(share["amount_cents"] for share in paid["split"]["shares"]), quote.total_cents)


class OfficialSchemaTests(unittest.TestCase):
    def test_official_store_schema(self):
        store = parse_store({
            "storeCode": "S1", "beCode": "BE1", "storeName": "测试餐厅",
            "address": "测试地址", "distanceText": "1.2km", "businessStatus": True,
            "businessStartTime": "00:00", "businessEndTime": "23:59",
        })
        self.assertEqual((store.code, store.be_code), ("S1", "BE1"))
        self.assertAlmostEqual(store.distance_km or 0, 1.2)

    def test_official_menu_schema(self):
        menu = parse_menu({
            "categories": [{"name": "人气热卖", "meals": [{"code": "920215", "tags": []}]}],
            "meals": {
                "920215": {"name": "培根安格斯厚牛堡大套餐", "currentPrice": "55.5"},
                "D1": {"name": "无糖可口可乐中杯", "currentPrice": "6"},
            },
        })
        self.assertEqual(menu[0].price_cents, 5550)
        self.assertEqual(menu[1].category, "drink")

    def test_official_price_schema_uses_cents(self):
        item = MenuItem("M1", "测试套餐", "main", 1600)
        lines = (LineItem(item, 1),)
        quote = parse_price({
            "productOriginalPrice": 1600, "productPrice": 1600,
            "deliveryOriginalPrice": 600, "deliveryPrice": 600,
            "discount": 0, "price": 2200, "tablewarePrice": 10,
            "takeWayList": [{"takeWayCode": "counter", "takeWayName": "柜台"}],
        }, lines, ())
        self.assertEqual(quote.total_cents, 2200)
        self.assertEqual(quote.service_fee_cents, 10)
        self.assertEqual(take_way_code(quote), "counter")

    def test_official_coupon_and_item_schema(self):
        coupons = parse_coupons([{
            "title": "测试券", "couponId": "CID", "couponCode": "CCODE",
            "tradeDateTime": "2025-04-21 23:23:00-2026-12-31 23:59:59",
            "products": [{"productCode": "M1", "productName": "测试"}],
        }])
        item = MenuItem("M1", "测试", "main", 1000)
        lines = (LineItem(item, 2),)
        assignments = coupon_assignments(lines, coupons, PickupMethod.SELF_PICKUP, datetime.fromisoformat("2026-10-24T23:30:00"))
        payload = items_payload(lines, assignments[1])
        self.assertEqual(payload[0]["couponId"], "CID")
        self.assertEqual(payload[0]["couponCode"], "CCODE")

    def test_official_create_order_schema(self):
        stores, menu, _, target = demo_data()
        lines = (LineItem(menu[0], 1),)
        quote = local_quote(lines, (), stores[0], PickupMethod.SELF_PICKUP)
        order = parse_created_order({
            "orderId": "ORDER1", "payId": "PAY1", "payH5Url": "https://example.com/pay",
            "orderDetail": {
                "orderStatus": "待支付", "realTotalAmount": "18",
                "productPrice": "18", "realDeliveryPrice": "0",
                "totalDiscountAmount": "0", "createTime": "2026-10-24 23:30:00",
            },
        }, stores[0], PickupMethod.SELF_PICKUP, quote)
        self.assertEqual(order.payment_url, "https://example.com/pay")
        self.assertEqual(order.quote.total_cents, 1800)
        self.assertEqual(order.status, "待支付")


class ConfigurationAndWorkflowTests(unittest.TestCase):
    def test_placeholder_token_rejected(self):
        with patch.dict("os.environ", {"MCD_MCP_TOKEN": ""}):
            with self.assertRaises(AppError):
                require_token()

    def test_budget_validation(self):
        self.assertEqual(parse_budget("199.99"), 19999)
        for value in ("-1", "1.001", "NaN", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_budget(value)

    def test_order_duplicate_guard(self):
        stores, menu, _, _ = demo_data()
        quote = local_quote((LineItem(menu[0], 1),), (), stores[0], PickupMethod.SELF_PICKUP)
        guard = OrderGuard()
        fingerprint = guard.before_create(stores[0], PickupMethod.SELF_PICKUP, quote)
        order = Order("DEMO-1", stores[0].code, PickupMethod.SELF_PICKUP, quote, "DEMO", None, None, True)
        guard.after_create(fingerprint, order)
        with self.assertRaises(AppError):
            guard.before_create(stores[0], PickupMethod.SELF_PICKUP, quote)

    def test_complete_demo_without_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                result = run_demo(assume_yes=True, output=output)
            self.assertTrue(output.exists())
            self.assertEqual(result["mode"], "demo")
            self.assertTrue(result["order"].is_demo)
            self.assertIsNone(result["order"].payment_url)
            self.assertEqual(result["quote"].total_cents, 18700)


class RealWorkflowFakeTests(unittest.TestCase):
    """使用官方响应结构验证真实流程；不联网、不创建真实订单。"""

    def test_real_self_pickup_workflow_calls_create_once(self):
        import io
        from contextlib import redirect_stdout

        class FakeMCP:
            def __init__(self):
                self.calls = []
                self.last_total = 0
                self.create_arguments = None

            def api(self, name, arguments=None):
                arguments = arguments or {}
                self.calls.append((name, arguments))
                if name == "now-time-info":
                    return {"datetime": "2026-10-24T23:30:00"}
                if name == "query-nearby-stores":
                    self.assert_query = arguments
                    return [{
                        "storeCode": "S1", "beCode": "BE1", "storeName": "真实结构测试店",
                        "address": "测试地址", "distanceText": "0.5km", "businessStatus": True,
                        "businessStartTime": "00:00", "businessEndTime": "00:00",
                    }]
                if name == "query-meals":
                    return {
                        "categories": [{"name": "全部", "meals": [{"code": code} for code in ("M1", "S1", "D1")]}],
                        "meals": {
                            "M1": {"name": "板烧鸡腿堡", "currentPrice": "15"},
                            "S1": {"name": "中薯条", "currentPrice": "5"},
                            "D1": {"name": "可乐", "currentPrice": "3"},
                        },
                    }
                if name == "query-store-coupons":
                    return []
                if name in {"auto-bind-coupons", "query-my-coupons"}:
                    return {}
                if name == "calculate-price":
                    prices = {"M1": 1500, "S1": 500, "D1": 300}
                    product = sum(prices[item["productCode"]] * item["quantity"] for item in arguments["items"])
                    tableware = 10 if arguments.get("needTableware") else 0
                    self.last_total = product + tableware
                    return {
                        "productPrice": product, "deliveryPrice": 0, "discount": 0,
                        "price": self.last_total, "tablewarePrice": tableware,
                        "takeWayList": [{"takeWayCode": "counter", "takeWayName": "柜台取餐"}],
                    }
                if name == "create-order":
                    self.create_arguments = arguments
                    return {
                        "orderId": "ORDER-FAKE", "payId": "PAY-FAKE",
                        "payH5Url": "https://example.com/real-structure-pay",
                        "orderDetail": {
                            "orderStatus": "待支付",
                            "realTotalAmount": str(Decimal(self.last_total) / 100),
                            "productPrice": str(Decimal(self.last_total - 10) / 100),
                            "realDeliveryPrice": "0", "totalDiscountAmount": "0",
                            "createTime": "2026-10-24 23:30:00",
                        },
                    }
                raise AssertionError("未预期工具：" + name)

        fake = FakeMCP()
        answers = iter(["8", "200", "1", "1", "", "南京市", "科技园", "", CONFIRM_TEXT, ""])
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(module, "MCPClient", return_value=fake), \
                patch.object(module, "require_token", return_value="test-token"), \
                patch("builtins.input", side_effect=lambda prompt="": next(answers)), \
                patch.object(Path, "cwd", return_value=Path(directory)), \
                redirect_stdout(io.StringIO()):
            result = run_real()
        create_calls = [call for call in fake.calls if call[0] == "create-order"]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(fake.create_arguments["takeWayCode"], "counter")
        self.assertEqual(fake.create_arguments["beType"], 1)
        self.assertNotIn("orderType", fake.create_arguments)
        self.assertNotIn("addressId", fake.create_arguments)
        self.assertEqual(result["order"].payment_url, "https://example.com/real-structure-pay")
        self.assertEqual(result["bills"]["actual"]["status"], "待确认")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="深夜团餐助手：真实麦当劳 MCP + 独立演示模式")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test", help="运行内置测试，不联网")
    demo = sub.add_parser("demo", help="运行独立演示，不联网")
    demo.add_argument("--yes", action="store_true", help="自动确认演示订单并生成演示凭证")
    demo.add_argument("--output", type=Path, help="演示结果 JSON 路径")
    sub.add_parser("real", help="进入真实 MCP 点餐流程")
    sub.add_parser("list-tools", help="列出真实 MCP 工具与 inputSchema")
    settle = sub.add_parser("settle", help="支付后查询真实订单并生成实付/分摊账单")
    settle.add_argument("order_id")
    settle.add_argument("--names", help="分摊成员，逗号分隔")
    call = sub.add_parser("call-tool", help="直接调用真实工具；请勿未确认就调用 create-order")
    call.add_argument("name")
    call.add_argument("arguments", nargs="?", default="{}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "self-test":
            suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1
        if args.command == "demo":
            run_demo(args.yes, args.output)
            return 0
        if args.command == "real":
            run_real()
            return 0
        if args.command == "settle":
            run_settle(args.order_id, args.names)
            return 0
        client = MCPClient()
        if args.command == "list-tools":
            print(json.dumps(client.list_tools(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "call-tool":
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("call-tool 参数必须是 JSON 对象")
            print(json.dumps(decode_content(client.call_tool(args.name, arguments)), ensure_ascii=False, indent=2))
            return 0
        raise ValueError("未知命令")
    except (AppError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消；程序不会自动重试或创建订单。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

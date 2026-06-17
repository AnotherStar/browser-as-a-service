"""Ozon-specific helpers: open a product page and pull the price block."""
from __future__ import annotations

import re

from .engine import _eval_json

_PRICE_JS = r"""
(function(){
  const out = {title: document.title, priceText: null, allPrices: []};
  const w = document.querySelector('[data-widget="webPrice"]')
        || document.querySelector('[data-widget="webSale"]');
  if (w) out.priceText = w.innerText.replace(/\s+/g, ' ').trim();
  const scope = (w ? w.innerText : document.body.innerText);
  const re = /(\d[\d\s ]*)\s*₽/g; let m;
  while ((m = re.exec(scope)) !== null) {
    const n = parseInt(m[1].replace(/[\s ]/g, ''), 10);
    if (!isNaN(n)) out.allPrices.push(n);
  }
  return out;
})()
"""


def _parse_int(token: str) -> int | None:
    digits = re.sub(r"[^\d]", "", token)
    return int(digits) if digits else None


def pick_prices(price_text: str | None, all_prices: list[int]) -> tuple[int | None, int | None]:
    """Best-effort: (regular_price, card_price).

    Ozon shows the discounted 'with Ozon Card' price first (smaller) and the
    regular price second. We return the larger as regular and the smaller,
    when there are two distinct values, as the card price."""
    prices = [p for p in all_prices if p and p > 0]
    if not prices:
        return None, None
    if len(prices) == 1:
        return prices[0], None
    card = min(prices[:3])
    regular = max(prices[:3])
    if card == regular:
        return regular, None
    return regular, card


async def extract_ozon_price(tab) -> dict:
    data = await _eval_json(tab, _PRICE_JS) or {}
    title = data.get("title")
    price_text = data.get("priceText")
    all_prices = data.get("allPrices") or []
    regular, card = pick_prices(price_text, all_prices)
    return {
        "title": title,
        "price_text": price_text,
        "price_value": regular,
        "card_price_value": card,
    }

"""Pydantic models for the scraping service.

These models define the public HTTP contract. FastAPI turns them into an
OpenAPI schema, from which a typesafe zod TS client is generated. Keep the
shapes explicit (Literal discriminators, defaults, descriptions) so the
generated client is pleasant to use from Node.js.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Proxy                                                                        #
# --------------------------------------------------------------------------- #


class Proxy(BaseModel):
    """A single proxy. Authenticated proxies are handled via CDP Fetch,
    so user/pass do NOT need to be embedded in the server URL."""

    server: str = Field(
        ...,
        description="Proxy address, e.g. 'http://1.2.3.4:8080' or '1.2.3.4:8080'.",
        examples=["http://1.2.3.4:8080"],
    )
    username: Optional[str] = Field(None, description="Proxy username (optional).")
    password: Optional[str] = Field(None, description="Proxy password (optional).")


class Cookie(BaseModel):
    """A cookie set before the first navigation. Useful for pinning a
    marketplace storefront to a region (e.g. Ozon/WB delivery region) so the
    prices and availability match what a buyer in that region sees."""

    name: str = Field(..., description="Cookie name.")
    value: str = Field(..., description="Cookie value.")
    domain: Optional[str] = Field(
        None,
        description="Cookie domain, e.g. '.ozon.ru'. Defaults to the "
        "registrable domain of the first navigated URL.",
        examples=[".ozon.ru"],
    )
    path: str = Field("/", description="Cookie path.")


# --------------------------------------------------------------------------- #
# Steps (a discriminated union on the `action` field)                         #
# --------------------------------------------------------------------------- #


class NavigateStep(BaseModel):
    action: Literal["navigate"] = "navigate"
    url: str = Field(..., description="URL to open.")
    new_tab: bool = Field(False, description="Open in a new tab instead of reusing.")
    settle_seconds: float = Field(
        3.0, ge=0, le=60, description="Seconds to wait after load for JS to settle."
    )


class WaitForStep(BaseModel):
    action: Literal["wait_for"] = "wait_for"
    selector: str = Field(..., description="CSS selector to wait for.")
    timeout_s: float = Field(15.0, gt=0, le=120)


class WaitForTextStep(BaseModel):
    action: Literal["wait_for_text"] = "wait_for_text"
    text: str = Field(..., description="Text to wait for anywhere on the page.")
    timeout_s: float = Field(15.0, gt=0, le=120)


class WaitForAnyStep(BaseModel):
    """Wait until ANY of the given selectors is present (whichever appears
    first), then continue. Avoids waiting the full timeout when e.g. an
    out-of-stock block shows up long before a (never-coming) price widget."""

    action: Literal["wait_for_any"] = "wait_for_any"
    selectors: list[str] = Field(
        ..., min_length=1, description="CSS selectors; the first present one wins."
    )
    timeout_s: float = Field(15.0, gt=0, le=120)


class SleepStep(BaseModel):
    action: Literal["sleep"] = "sleep"
    seconds: float = Field(..., gt=0, le=60)


class ClickStep(BaseModel):
    action: Literal["click"] = "click"
    selector: str = Field(..., description="CSS selector of element to click.")
    timeout_s: float = Field(15.0, gt=0, le=120)


class ScrollStep(BaseModel):
    action: Literal["scroll"] = "scroll"
    direction: Literal["down", "up"] = "down"
    amount: int = Field(50, description="Scroll amount (percent of viewport) per step.")
    times: int = Field(1, ge=1, le=50, description="How many times to scroll.")


class ExtractStep(BaseModel):
    """Extract values from the DOM via a CSS selector. Result is stored in
    the response `data` under `name`."""

    action: Literal["extract"] = "extract"
    name: str = Field(..., description="Key under which the result is returned.")
    selector: str = Field(..., description="CSS selector.")
    kind: Literal["text", "html", "attr"] = Field(
        "text", description="What to read: visible text, innerHTML, or an attribute."
    )
    attr: Optional[str] = Field(
        None, description="Attribute name (required when kind='attr')."
    )
    many: bool = Field(
        False, description="If true, returns a list for all matches instead of the first."
    )


class FindTextStep(BaseModel):
    """Find an element by its text (nodriver's smart text search) and store
    the enclosing element's text."""

    action: Literal["find_text"] = "find_text"
    name: str = Field(..., description="Key under which the result is returned.")
    text: str = Field(..., description="Text to search for.")
    timeout_s: float = Field(10.0, gt=0, le=120)


class EvalStep(BaseModel):
    """Run arbitrary JavaScript in the page and capture the returned value."""

    action: Literal["eval"] = "eval"
    expression: str = Field(..., description="JS expression; its value is captured.")
    name: Optional[str] = Field(
        None, description="Key to store the result under (omit to discard)."
    )


class ScreenshotStep(BaseModel):
    action: Literal["screenshot"] = "screenshot"
    name: str = Field(..., description="Key under which the base64 image is returned.")
    full_page: bool = Field(False, description="Capture the full page vs viewport.")


Step = Annotated[
    Union[
        NavigateStep,
        WaitForStep,
        WaitForAnyStep,
        WaitForTextStep,
        SleepStep,
        ClickStep,
        ScrollStep,
        ExtractStep,
        FindTextStep,
        EvalStep,
        ScreenshotStep,
    ],
    Field(discriminator="action"),
]


# --------------------------------------------------------------------------- #
# Generic /run                                                                 #
# --------------------------------------------------------------------------- #


class RunRequest(BaseModel):
    steps: list[Step] = Field(..., min_length=1, description="Ordered list of steps.")
    start_url: Optional[str] = Field(
        None,
        description="Convenience: navigate here before running steps "
        "(equivalent to a leading navigate step).",
    )
    proxy: Optional[Proxy] = Field(None, description="Optional proxy for this run.")
    use_proxy: bool = Field(
        False,
        description="Route this run through an Asocks residential proxy "
        "(https://asocks.com). Ignored when an explicit `proxy` is given. "
        "Requires ASOCKS_API_KEY on the server.",
    )
    proxy_country: Optional[str] = Field(
        None,
        description="ISO country code for the Asocks proxy, e.g. 'RU'. "
        "Omit to use any available port. Only used when `use_proxy` is true.",
        examples=["RU"],
    )
    cookies: Optional[list[Cookie]] = Field(
        None,
        description="Cookies set before the first navigation (e.g. region "
        "pinning). Domain defaults to the start_url's registrable domain.",
    )
    headless: bool = Field(
        False,
        description="Headless Chrome. NOTE: Ozon blocks headless — keep this false.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "start_url": "https://www.ozon.ru/product/example-123456/",
                    "steps": [
                        {"action": "wait_for", "selector": "[data-widget=webPrice]"},
                        {
                            "action": "extract",
                            "name": "price",
                            "selector": "[data-widget=webPrice]",
                            "kind": "text",
                        },
                        {"action": "extract", "name": "title", "selector": "h1"},
                    ],
                }
            ]
        }
    }


class StepResult(BaseModel):
    index: int
    action: str
    ok: bool
    error: Optional[str] = None


class RunResponse(BaseModel):
    ok: bool
    final_url: Optional[str] = None
    elapsed_ms: int
    data: dict[str, Any] = Field(
        default_factory=dict, description="Extracted values keyed by step `name`."
    )
    steps: list[StepResult] = Field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Convenience: Ozon price                                                      #
# --------------------------------------------------------------------------- #


class OzonPriceRequest(BaseModel):
    url: str = Field(..., description="Ozon product page URL.")
    proxy: Optional[Proxy] = None
    use_proxy: bool = Field(
        False,
        description="Route through an Asocks residential proxy. Ignored when "
        "an explicit `proxy` is given. Requires ASOCKS_API_KEY on the server.",
    )
    proxy_country: Optional[str] = Field(
        None,
        description="ISO country code for the Asocks proxy, e.g. 'RU'. "
        "Omit for any available port. Only used when `use_proxy` is true.",
        examples=["RU"],
    )
    cookies: Optional[list[Cookie]] = Field(
        None,
        description="Cookies set before navigation (e.g. region pinning).",
    )
    headless: bool = Field(False, description="Keep false; Ozon blocks headless.")
    use_cache: bool = Field(True, description="Serve a recent cached value if present.")


class OzonPriceResponse(BaseModel):
    ok: bool
    url: str
    title: Optional[str] = None
    price_text: Optional[str] = Field(
        None, description="Raw price block text as shown on the page."
    )
    price_value: Optional[int] = Field(
        None, description="Best-effort main price parsed to an integer (rubles)."
    )
    card_price_value: Optional[int] = Field(
        None, description="Best-effort 'Ozon Card' price parsed to an integer."
    )
    cached: bool = False
    fetched_at: Optional[str] = Field(None, description="ISO timestamp of fetch.")
    elapsed_ms: int = 0
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    browser_ready: bool
    chrome_path: Optional[str] = None

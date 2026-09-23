# ba_meta require api 9
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import babase
import bauiv1 as bui
import bascenev1 as bs
from bauiv1lib import party


# ============================================================
# Bon Buy Cheat
# BombSquad 1.7.62 / API 9
#
# Uses the same BSLife /mod API path used by the supplied
# BSLife mod. The "$" menu command is discovered dynamically
# from the BSLife stats/config response instead of hardcoding
# an unknown sales-query name.
# ============================================================

API_BASE = "https://api.bslife.ir"
STATS_URL = API_BASE + "/mod/stats"

MAX_SLOTS = 6
POLL_SECONDS = 0.8
BUY_DELAY_SECONDS = 0.03

CONFIG_KEY = "Bon Buy Cheat Config"

# code s123 -> maximum total listing price
_DEFAULT_LIMITS = ["", "", "", "", "", ""]
_DEFAULT_PRICES = ["", "", "", "", "", ""]

_enabled = False
_running = True
_scan_lock = threading.Lock()
_seen_codes: set[str] = set()
_party_window_ref = None
_panel_ref = None
_scan_timer = None

_item_widgets: list[Any] = []
_price_widgets: list[Any] = []
_item_labels: list[Any] = []

_sales_query = None
_last_scan_error = None


# ---------- small helpers ----------

def _screen(msg: str, color=(1.0, 1.0, 1.0)) -> None:
    try:
        bui.screenmessage(msg, color=color)
    except Exception:
        pass


def _get_config() -> dict:
    try:
        cfg = babase.app.config
        data = cfg.get(CONFIG_KEY, {})
        if not isinstance(data, dict):
            data = {}
        items = list(data.get("items", _DEFAULT_LIMITS))
        prices = list(data.get("prices", _DEFAULT_PRICES))
        while len(items) < MAX_SLOTS:
            items.append("")
        while len(prices) < MAX_SLOTS:
            prices.append("")
        return {
            "items": [str(x) for x in items[:MAX_SLOTS]],
            "prices": [str(x) for x in prices[:MAX_SLOTS]],
        }
    except Exception:
        return {"items": list(_DEFAULT_LIMITS), "prices": list(_DEFAULT_PRICES)}


def _save_config() -> None:
    try:
        cfg = babase.app.config
        cfg[CONFIG_KEY] = {
            "items": [
                str(bui.textwidget(query=w)).strip()
                if w is not None and w.exists() else ""
                for w in _item_widgets
            ],
            "prices": [
                str(bui.textwidget(query=w)).strip()
                if w is not None and w.exists() else ""
                for w in _price_widgets
            ],
        }
        cfg.commit()
    except Exception:
        pass


def _get_account_token() -> tuple[str, str, str]:
    """Return (account_id_b64, token, token_file)."""
    display = ""
    try:
        display = str(bui.app.plus.get_v1_account_display_string())
    except Exception:
        pass

    account_id_b64 = base64.b64encode(display.encode("utf-8")).decode("utf-8")
    token_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), account_id_b64
    )

    token = ""
    try:
        if os.path.exists(token_file):
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
    except Exception:
        pass

    return account_id_b64, token, token_file


def _request(url: str, token: str = "", data: bytes = b"") -> bytes:
    account_id_b64, _, _ = _get_account_token()
    headers = {
        "X-AccountId": account_id_b64,
        "Authorization": "Bearer " + token,
        "Accept-Encoding": "gzip",
    }
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    response = urllib.request.urlopen(req, timeout=5)

    # The BSLife server can rotate the token through this header.
    new_token = response.headers.get("X-N-Token")
    if new_token:
        try:
            _, _, token_file = _get_account_token()
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(new_token)
        except Exception:
            pass

    raw = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    response.close()
    return raw


def _request_json(url: str) -> Any:
    _, token, _ = _get_account_token()
    raw = _request(url, token)
    return json.loads(raw)


def _get_stats_config() -> dict | None:
    """Get BSLife's live config. The supplied mod stores this as response['c']."""
    try:
        _, token, _ = _get_account_token()

        # The supplied BSLife mod sends form data here.
        raw = _request(STATS_URL, token, urllib.parse.urlencode([]).encode("utf-8"))

        # /mod/stats returns base64 encoded JSON.
        try:
            data = json.loads(base64.b64decode(raw))
        except Exception:
            data = json.loads(raw)

        # Keep the rotated token if present; _request already saved it.
        if isinstance(data, dict) and isinstance(data.get("c"), dict):
            return data["c"]

        # Some versions can expose the config directly.
        if isinstance(data, dict) and isinstance(data.get("m"), dict):
            return data

    except Exception as exc:
        global _last_scan_error
        _last_scan_error = str(exc)

    return None


def _find_sales_query_from_stats(config: dict) -> str | None:
    """
    In the supplied BSLife mod, config['m'] is the menu dictionary:
        visible_button_label -> menu command.

    We look specifically for the '$' button and then translate its
    query__ command into the /mod/<query> endpoint.
    """
    menu = config.get("m")
    if not isinstance(menu, dict):
        return None

    # Exact '$' first.
    candidates = []
    if "$" in menu:
        candidates.append(menu["$"])

    # Be tolerant of a server-side label containing the dollar sign.
    for label, command in menu.items():
        if "$" in str(label) and command not in candidates:
            candidates.append(command)

    for command in candidates:
        command = str(command)
        if command.startswith("query__"):
            return command.split("__", 1)[1]
    return None


def _ensure_sales_query() -> str | None:
    global _sales_query

    if _sales_query:
        return _sales_query

    config = _get_stats_config()
    if not config:
        return None

    _sales_query = _find_sales_query_from_stats(config)
    return _sales_query


def _query_sales() -> Any:
    query = _ensure_sales_query()
    if not query:
        return None

    _, token, _ = _get_account_token()
    raw = _request(API_BASE + "/mod/" + str(query), token)

    try:
        return json.loads(raw, object_pairs_hook=dict)
    except Exception:
        return None


# ---------- sales parser ----------

_CODE_RE = re.compile(r"(?<![A-Za-z0-9])s(\d+)(?![A-Za-z0-9])", re.I)
_PRICE_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)\s*([kK])?(?![A-Za-z0-9])"
)
_ITEM_RE = re.compile(
    r"s\d+\s*:\s*.*?([A-Za-z][A-Za-z0-9_]*)\s*[xX×]\s*(\d+)",
    re.I,
)
_SIMPLE_ITEM_RE = re.compile(
    r"\b(?:item|name)\s*[:=]\s*([A-Za-z][A-Za-z0-9_]*)",
    re.I,
)


def _price_to_number(value: str, suffix: str = "") -> float:
    value = value.replace(",", "").strip()
    number = float(value)
    if suffix.lower() == "k":
        number *= 1000.0
    return number


def _flatten_text(obj: Any) -> list[str]:
    out: list[str] = []

    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.append(k)
            out.extend(_flatten_text(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_flatten_text(v))

    return out


def _resolve_bslife_value(value: Any, row: Any, depth: int = 0) -> list[str]:
    """Resolve the compact row-reference format used by BSLife query data."""
    if depth > 8:
        return []

    if isinstance(value, str):
        return [value]

    if isinstance(value, (int, float)):
        return [str(value)]

    if isinstance(value, (list, tuple)):
        # The supplied BSLife renderer uses [row_key, value_key]
        # references and resolves them through row['r'][...].
        if (
            len(value) == 2
            and isinstance(row, dict)
            and isinstance(row.get("r"), (dict, list, tuple))
        ):
            try:
                container = row["r"]
                first = value[0]
                second = value[1]
                if isinstance(container, dict):
                    resolved = container[first]
                else:
                    resolved = container[int(first)]
                if isinstance(resolved, (dict, list, tuple)):
                    if isinstance(resolved, dict):
                        resolved = resolved[second]
                    else:
                        resolved = resolved[int(second)]
                return _resolve_bslife_value(resolved, row, depth + 1)
            except Exception:
                pass

        result: list[str] = []
        for child in value:
            result.extend(_resolve_bslife_value(child, row, depth + 1))
        return result

    if isinstance(value, dict):
        result: list[str] = []
        for key, child in value.items():
            if isinstance(key, str):
                result.append(key)
            result.extend(_resolve_bslife_value(child, row, depth + 1))
        return result

    return []


def _row_texts(row: Any) -> list[str]:
    texts = _resolve_bslife_value(row, row)

    # De-duplicate while preserving order.
    result = []
    seen = set()
    for text in texts:
        text = str(text).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _parse_sale_from_row(row: Any) -> dict | None:
    texts = _row_texts(row)
    if not texts:
        return None

    combined = " ".join(texts)
    code_match = _CODE_RE.search(combined)
    if not code_match:
        return None

    code = "s" + code_match.group(1)

    # Prefer the exact screenshot-style format:
    # s403: [emoji] k x1 -> 4.7k
    item = None
    quantity = None

    item_match = _ITEM_RE.search(combined)
    if item_match:
        item = item_match.group(1).strip()
        quantity = int(item_match.group(2))
    else:
        item_match = _SIMPLE_ITEM_RE.search(combined)
        if item_match:
            item = item_match.group(1).strip()

    # Price: prefer the number after an arrow because the same row can
    # contain other numbers (seller id, quantity, etc.).
    price = None
    arrow_match = re.search(
        r"(?:->|→)\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?",
        combined,
    )
    if arrow_match:
        price = _price_to_number(arrow_match.group(1), arrow_match.group(2) or "")
    else:
        # If the API exposes price as a separate field, this catches it.
        # We use the last numeric token in the row as a conservative fallback.
        matches = list(_PRICE_RE.finditer(combined))
        if matches:
            try:
                price = _price_to_number(
                    matches[-1].group(1),
                    matches[-1].group(2) or "",
                )
            except Exception:
                price = None

    if item is None or price is None:
        return None

    return {
        "code": code.lower(),
        "item": item.lower(),
        "quantity": quantity,
        "price": price,
        "raw": combined,
    }


def _extract_sales(data: Any) -> list[dict]:
    """
    Primary path: BSLife query responses use a top-level 'list' of rows.
    Fallback: recursively inspect list-like structures for rows containing
    s<number> plus item/price information.
    """
    sales: list[dict] = []

    if isinstance(data, dict) and isinstance(data.get("list"), list):
        for row in data["list"]:
            sale = _parse_sale_from_row(row)
            if sale:
                sales.append(sale)

    if not sales:
        def walk(obj: Any) -> None:
            if isinstance(obj, list):
                sale = _parse_sale_from_row(obj)
                if sale:
                    sales.append(sale)
                    return
                for child in obj:
                    walk(child)
            elif isinstance(obj, dict):
                for value in obj.values():
                    walk(value)

        walk(data)

    # De-duplicate by purchase code.
    unique: dict[str, dict] = {}
    for sale in sales:
        unique[sale["code"]] = sale

    return list(unique.values())


# ---------- auto buy ----------

def _configured_limits() -> dict[str, float]:
    result: dict[str, float] = {}

    for i in range(MAX_SLOTS):
        item = ""
        price = ""

        try:
            if i < len(_item_widgets) and _item_widgets[i] is not None:
                item = str(bui.textwidget(query=_item_widgets[i])).strip().lower()
            if i < len(_price_widgets) and _price_widgets[i] is not None:
                price = str(bui.textwidget(query=_price_widgets[i])).strip()
        except Exception:
            continue

        if not item or not price:
            continue

        try:
            limit = float(price.replace(",", ""))
        except Exception:
            continue

        if limit > 0:
            result[item] = limit

    return result


def _send_buy(code: str, item: str, price: float) -> None:
    if not _enabled:
        return

    try:
        bs.chatmessage("b " + code)

        # Exact requested delay: 0.03 seconds.
        def send_confirm() -> None:
            try:
                if _enabled:
                    bs.chatmessage("1")
            except Exception:
                pass

        babase.apptimer(BUY_DELAY_SECONDS, send_confirm)
        _screen(
            "BUY: %s  %s  %.0f" % (item, code, price),
            color=(1.0, 0.85, 0.1),
        )
    except Exception:
        pass


def _scan_once() -> None:
    if not _enabled:
        return

    # Prevent overlapping HTTP scans.
    if not _scan_lock.acquire(False):
        return

    try:
        limits = _configured_limits()
        if not limits:
            return

        sales = _extract_sales(_query_sales())
        if not sales:
            return

        for sale in sales:
            if not _enabled:
                break

            code = sale["code"]
            item = sale["item"]
            price = float(sale["price"])

            limit = limits.get(item)
            if limit is None:
                continue

            # Strictly below the configured maximum, as requested.
            if price >= limit:
                continue

            if code in _seen_codes:
                continue

            _seen_codes.add(code)
            _send_buy(code, item, price)

    except Exception as exc:
        global _last_scan_error
        _last_scan_error = str(exc)
    finally:
        _scan_lock.release()


def _scan_thread() -> None:
    try:
        _scan_once()
    except Exception:
        pass


# ---------- panel ----------

def _panel_exists() -> bool:
    global _panel_ref
    try:
        return _panel_ref is not None and _panel_ref.exists()
    except Exception:
        return False


def _close_panel() -> None:
    global _panel_ref, _item_widgets, _price_widgets, _item_labels
    try:
        if _panel_ref is not None and _panel_ref.exists():
            bui.containerwidget(edit=_panel_ref, transition="out_scale")
    except Exception:
        pass
    _panel_ref = None
    _item_widgets = []
    _price_widgets = []
    _item_labels = []


def _toggle_enabled() -> None:
    global _enabled

    _enabled = not _enabled

    _screen(
        "Buy cheat ON" if _enabled else "Buy cheat OFF",
        color=(0.2, 1.0, 0.2) if _enabled else (1.0, 0.35, 0.35),
    )

    _update_toggle_button()

    if _enabled:
        # Immediate first scan; timer keeps it live afterwards.
        threading.Thread(target=_scan_thread, daemon=True).start()


def _update_toggle_button() -> None:
    try:
        if not _panel_exists():
            return
        btn = getattr(_toggle_enabled, "_button", None)
        if btn is not None and btn.exists():
            bui.buttonwidget(
                edit=btn,
                label="ON" if _enabled else "OFF",
                color=(0.10, 0.65, 0.16) if _enabled else (0.55, 0.16, 0.16),
            )
    except Exception:
        pass


def _sync_pair_labels() -> None:
    for i in range(MAX_SLOTS):
        try:
            item = str(bui.textwidget(query=_item_widgets[i])).strip()
        except Exception:
            item = ""

        text = "for: " + (item if item else "empty")
        try:
            if _item_labels[i].exists():
                bui.textwidget(edit=_item_labels[i], text=text)
        except Exception:
            pass


def _open_panel() -> None:
    global _panel_ref, _item_widgets, _price_widgets, _item_labels

    if _panel_exists():
        _close_panel()
        return

    _item_widgets = []
    _price_widgets = []
    _item_labels = []

    saved = _get_config()

    root = bui.containerwidget(
        parent=bui.get_special_widget("overlay_stack"),
        size=(780, 650),
        position=(0, -10),
        scale=1.0,
        transition="in_scale",
        background=True,
        color=(0.075, 0.075, 0.095),
        stack_offset=(0, 0),
    )
    _panel_ref = root

    # Decorative header.
    bui.textwidget(
        parent=root,
        position=(0, 555),
        size=(780, 55),
        text="☠",
        scale=3.0,
        color=(0.92, 0.86, 0.20),
        h_align="center",
        v_align="center",
    )
    bui.textwidget(
        parent=root,
        position=(0, 515),
        size=(780, 42),
        text="Bon Buy Cheat",
        scale=1.45,
        color=(1.0, 0.84, 0.15),
        h_align="center",
        v_align="center",
    )

    bui.buttonwidget(
        parent=root,
        position=(690, 570),
        size=(55, 45),
        label="X",
        text_scale=1.0,
        color=(0.42, 0.12, 0.12),
        button_type="square",
        on_activate_call=_close_panel,
    )

    toggle = bui.buttonwidget(
        parent=root,
        position=(65, 455),
        size=(650, 48),
        label="ON" if _enabled else "OFF",
        text_scale=1.1,
        color=(0.10, 0.65, 0.16) if _enabled else (0.55, 0.16, 0.16),
        button_type="square",
        on_activate_call=_toggle_enabled,
    )
    _toggle_enabled._button = toggle

    bui.textwidget(
        parent=root,
        position=(65, 425),
        size=(650, 25),
        text="AUTO BUY",
        scale=0.75,
        color=(0.75, 0.75, 0.80),
        h_align="center",
        v_align="center",
    )

    # Column headings.
    bui.textwidget(
        parent=root,
        position=(70, 380),
        size=(300, 32),
        text="Item",
        scale=0.95,
        color=(1.0, 0.88, 0.20),
        h_align="center",
        v_align="center",
    )
    bui.textwidget(
        parent=root,
        position=(410, 380),
        size=(300, 32),
        text="Maximum price",
        scale=0.95,
        color=(1.0, 0.88, 0.20),
        h_align="center",
        v_align="center",
    )

    row_y = 330
    row_gap = 52

    for i in range(MAX_SLOTS):
        # Slot number.
        bui.textwidget(
            parent=root,
            position=(20, row_y + 5),
            size=(35, 28),
            text=str(i + 1),
            scale=0.65,
            color=(0.55, 0.55, 0.60),
            h_align="center",
            v_align="center",
        )

        item_edit = bui.textwidget(
            parent=root,
            position=(70, row_y),
            size=(280, 36),
            editable=True,
            selectable=True,
            text=saved["items"][i],
            color=(0.92, 0.92, 0.96),
            textcolor=(0.08, 0.08, 0.10),
            v_align="center",
            h_align="center",
            padding=5,
        )
        _item_widgets.append(item_edit)

        # Tiny relation label beside/above each price field.
        relation = bui.textwidget(
            parent=root,
            position=(365, row_y + 8),
            size=(95, 22),
            text="for: " + (saved["items"][i].strip() or "empty"),
            scale=0.48,
            color=(0.65, 0.70, 0.78),
            h_align="right",
            v_align="center",
        )
        _item_labels.append(relation)

        price_edit = bui.textwidget(
            parent=root,
            position=(470, row_y),
            size=(240, 36),
            editable=True,
            selectable=True,
            text=saved["prices"][i],
            color=(0.92, 0.92, 0.96),
            textcolor=(0.08, 0.08, 0.10),
            v_align="center",
            h_align="center",
            padding=5,
        )
        _price_widgets.append(price_edit)

        row_y -= row_gap

    # Save settings.
    bui.buttonwidget(
        parent=root,
        position=(70, 25),
        size=(300, 48),
        label="SAVE",
        text_scale=0.9,
        color=(0.12, 0.42, 0.68),
        button_type="square",
        on_activate_call=_save_config,
    )

    bui.buttonwidget(
        parent=root,
        position=(410, 25),
        size=(300, 48),
        label="CLOSE",
        text_scale=0.9,
        color=(0.34, 0.34, 0.38),
        button_type="square",
        on_activate_call=_close_panel,
    )

    _sync_pair_labels()

    # Update "for: item" labels whenever the panel gets a moment to breathe.
    def refresh_labels() -> None:
        if _panel_exists():
            _sync_pair_labels()

    babase.apptimer(0.25, refresh_labels)


# ---------- chat-page button ----------

def _install_chat_button(window: Any) -> None:
    try:
        root = window.get_root_widget()

        # Avoid duplicates if PartyWindow is reconstructed.
        old = getattr(window, "_bon_buy_button", None)
        if old is not None:
            try:
                if old.exists():
                    old.delete()
            except Exception:
                pass

        # A compact yellow dollar button near the top of the chat window.
        button = bui.buttonwidget(
            parent=root,
            position=(355, 365),
            size=(52, 46),
            label="$",
            text_scale=1.25,
            textcolor=(0.12, 0.08, 0.0),
            color=(0.95, 0.70, 0.05),
            button_type="square",
            autoselect=False,
            on_activate_call=_open_panel,
        )

        window._bon_buy_button = button
        _party_window_ref = window
    except Exception:
        pass


# Hook PartyWindow.__init__ without replacing the rest of the chat system.
_original_party_init = party.PartyWindow.__init__


def _party_init_hook(self: Any, *args: Any, **kwargs: Any) -> None:
    _original_party_init(self, *args, **kwargs)
    babase.apptimer(
        0.05,
        lambda: _install_chat_button(self),
    )


party.PartyWindow.__init__ = _party_init_hook


# ---------- background scanner ----------

def _scanner_tick() -> None:
    if not _running:
        return

    if _enabled:
        threading.Thread(target=_scan_thread, daemon=True).start()


try:
    _scan_timer = babase.AppTimer(POLL_SECONDS, _scanner_tick, repeat=True)
except Exception:
    try:
        _scan_timer = bui.AppTimer(POLL_SECONDS, _scanner_tick, repeat=True)
    except Exception:
        _scan_timer = None


# ba_meta export babase.Plugin
class BonBuyCheatPlugin(babase.Plugin):
    pass

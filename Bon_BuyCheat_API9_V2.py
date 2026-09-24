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


# ba_meta export babase.Plugin
class BonBuyCheatPlugin(babase.Plugin):
    pass


# ============================================================
# Bon Buy Cheat V2
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


# ---------- BSLife sales backend ----------

def _get_stats_config() -> dict | None:
    """
    Read the REAL BSLife /mod/stats response.

    IMPORTANT:
    In the original BSLife mod, the menu commands are in the
    TOP-LEVEL 'm' key of the /mod/stats response.
    We must return the whole response, not response['c'].
    """
    try:
        _, token, _ = _get_account_token()

        raw = _request(
            STATS_URL,
            token,
            urllib.parse.urlencode([]).encode("utf-8"),
        )

        # /mod/stats returns base64 encoded JSON.
        try:
            decoded = base64.b64decode(raw)
            data = json.loads(decoded)
        except Exception:
            data = json.loads(raw)

        if isinstance(data, dict):
            return data

    except Exception as exc:
        global _last_scan_error
        _last_scan_error = "STATS: " + str(exc)

    return None


def _find_sales_query_from_stats(config: dict) -> str | None:
    """
    The original BSLife mod creates its buttons from:

        stats_response['m']

    The '$' button contains something like:

        query__SOMETHING

    We extract SOMETHING dynamically.
    """

    menu = config.get("m")

    if not isinstance(menu, dict):
        return None

    # Exact dollar button first.
    candidates = []

    if "$" in menu:
        candidates.append(menu["$"])

    # Also support labels which contain '$'.
    for label, command in menu.items():
        if "$" in str(label):
            if command not in candidates:
                candidates.append(command)

    for command in candidates:
        command = str(command)

        if command.startswith("query__"):
            return command.split("__", 1)[1]

    return None


def _ensure_sales_query() -> str | None:
    global _sales_query

    # Don't permanently cache a failed lookup.
    if _sales_query:
        return _sales_query

    config = _get_stats_config()

    if not config:
        return None

    query = _find_sales_query_from_stats(config)

    if query:
        _sales_query = query

    return query


def _query_sales() -> Any:
    """
    Ask the SAME BSLife API used by the original '$' sales button.

    This is not local-chat parsing.
    This is the central BSLife sales database endpoint.
    """

    query = _ensure_sales_query()

    if not query:
        global _last_scan_error
        _last_scan_error = "Could not find BSLife sales query"
        return None

    try:
        _, token, _ = _get_account_token()

        raw = _request(
            API_BASE + "/mod/" + str(query),
            token,
            urllib.parse.urlencode([]).encode("utf-8"),
        )

        # The original mod receives normal JSON here.
        try:
            return json.loads(
                raw,
                object_pairs_hook=dict,
            )
        except Exception:

            # Extra tolerance in case the server wraps the response.
            try:
                decoded = base64.b64decode(raw)
                return json.loads(
                    decoded,
                    object_pairs_hook=dict,
                )
            except Exception:
                _last_scan_error = "Invalid BSLife sales response"
                return None

    except Exception as exc:
        _last_scan_error = "SALES: " + str(exc)
        return None


# ---------- REAL BSLife row decoder ----------

_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])s(\d{2,4})(?![A-Za-z0-9])",
    re.I,
)

_PRICE_AFTER_ARROW_RE = re.compile(
    r"(?:->|→)\s*"
    r"(\d[\d,]*(?:\.\d+)?)"
    r"\s*([kKmM])?",
)

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(\d[\d,]*(?:\.\d+)?)"
    r"\s*([kKmM])?"
    r"(?![A-Za-z0-9])"
)


def _price_to_number(value: str, suffix: str = "") -> float:
    value = str(value).replace(",", "").strip()

    number = float(value)

    suffix = str(suffix or "").lower()

    if suffix == "k":
        number *= 1000.0
    elif suffix == "m":
        number *= 1000000.0

    return number


def _resolve_row_value(value: Any, row: Any, depth: int = 0) -> list[str]:
    """
    Decode the compact [row_index, value_index] references used
    by the REAL BSLife mod.

    The original renderer uses:

        row['r'][ref[0]][ref[1]]
    """

    if depth > 10:
        return []

    if isinstance(value, str):
        return [value]

    if isinstance(value, (int, float)):
        return [str(value)]

    if isinstance(value, (list, tuple)):

        # BSLife reference:
        # [row-index, value-index]
        if (
            len(value) == 2
            and isinstance(row, dict)
            and isinstance(row.get("r"), (list, tuple, dict))
        ):
            try:
                rows = row["r"]

                first = value[0]
                second = value[1]

                if isinstance(rows, dict):
                    resolved = rows[first]
                else:
                    resolved = rows[int(first)]

                if isinstance(resolved, dict):
                    resolved = resolved[second]

                elif isinstance(resolved, (list, tuple)):
                    resolved = resolved[int(second)]

                return _resolve_row_value(
                    resolved,
                    row,
                    depth + 1,
                )

            except Exception:
                pass

        result = []

        for child in value:
            result.extend(
                _resolve_row_value(
                    child,
                    row,
                    depth + 1,
                )
            )

        return result

    if isinstance(value, dict):

        result = []

        for key, child in value.items():

            if isinstance(key, str):
                result.append(key)

            result.extend(
                _resolve_row_value(
                    child,
                    row,
                    depth + 1,
                )
            )

        return result

    return []


def _get_real_row_text(row: Any) -> str:
    """
    Reconstruct the text exactly from BSLife's row structure.

    This is based on the same r/reference mechanism used by
    the original mod.php renderer.
    """

    if not isinstance(row, dict):
        return ""

    parts = []

    # BSLife's actual rendered fields live under 'r'.
    rdata = row.get("r")

    if isinstance(rdata, (list, tuple, dict)):

        if isinstance(rdata, dict):
            iterable = rdata.values()
        else:
            iterable = rdata

        for group in iterable:

            if isinstance(group, dict):
                values = group.values()

            elif isinstance(group, (list, tuple)):
                values = group

            else:
                values = [group]

            for value in values:

                if isinstance(value, (str, int, float)):
                    parts.append(str(value))

    # Also inspect other row fields because some versions expose
    # display information directly.
    for key in ("text", "name", "item", "price", "code"):

        value = row.get(key)

        if value is not None:
            parts.extend(
                _resolve_row_value(
                    value,
                    row,
                )
            )

    # Remove duplicates while keeping order.
    result = []
    seen = set()

    for part in parts:

        part = str(part).strip()

        if not part:
            continue

        if part not in seen:
            seen.add(part)
            result.append(part)

    return " ".join(result)


def _extract_sale_from_row(row: Any) -> dict | None:

    if not isinstance(row, dict):
        return None

    # First try the actual BSLife row representation.
    combined = _get_real_row_text(row)

    # Fallback: recursively resolve everything.
    if not combined:
        combined = " ".join(
            _resolve_row_value(
                row,
                row,
            )
        )

    if not combined:
        return None

    # --------------------------------------------------------
    # 1. SALE CODE
    # --------------------------------------------------------

    code_match = _CODE_RE.search(combined)

    if not code_match:
        return None

    code = "s" + code_match.group(1)

    # --------------------------------------------------------
    # 2. PRICE
    # --------------------------------------------------------

    price = None

    # The BSLife sales display puts the sale price after -> / →.
    arrow_match = _PRICE_AFTER_ARROW_RE.search(combined)

    if arrow_match:

        try:
            price = _price_to_number(
                arrow_match.group(1),
                arrow_match.group(2) or "",
            )
        except Exception:
            price = None

    # Fallback if the server response doesn't contain an arrow.
    if price is None:

        matches = list(
            _NUMBER_RE.finditer(combined)
        )

        # Ignore the sale code itself.
        for match in reversed(matches):

            try:

                candidate = _price_to_number(
                    match.group(1),
                    match.group(2) or "",
                )

                # Ignore suspiciously huge values.
                if candidate >= 0:
                    price = candidate
                    break

            except Exception:
                pass

    if price is None:
        return None

    # --------------------------------------------------------
    # 3. ITEM
    # --------------------------------------------------------

    item = None

    # Typical BSLife format:
    #
    # s123: ITEM x1 -> 2999
    #
    # Quantity is deliberately ignored.
    #
    item_match = re.search(
        r"s\d{2,4}\s*:\s*"
        r"(.*?)"
        r"\s*[xX×]\s*\d+"
        r"\s*(?:->|→)",
        combined,
        re.I,
    )

    if item_match:

        item = item_match.group(1).strip()

    else:

        # More tolerant form:
        #
        # s123: ITEM -> 2999
        #
        item_match = re.search(
            r"s\d{2,4}\s*:\s*"
            r"(.*?)"
            r"\s*(?:->|→)",
            combined,
            re.I,
        )

        if item_match:
            item = item_match.group(1).strip()

    if not item:

        # API versions which expose item/name separately.
        for key in ("item", "name"):

            value = row.get(key)

            if value is not None:

                vals = _resolve_row_value(
                    value,
                    row,
                )

                if vals:
                    item = str(vals[0]).strip()
                    break

    if not item:
        return None

    # Remove accidental sale-code text from item.
    item = re.sub(
        r"^s\d{2,4}\s*:\s*",
        "",
        item,
        flags=re.I,
    ).strip()

    # Remove quantity if it survived.
    item = re.sub(
        r"\s*[xX×]\s*\d+\s*$",
        "",
        item,
    ).strip()

    if not item:
        return None

    return {
        "code": code.lower(),
        "item": item.casefold(),
        "price": float(price),
        "raw": combined,
    }


def _extract_sales(data: Any) -> list[dict]:

    sales = []

    # REAL BSLife query response:
    #
    # {
    #     ...
    #     "list": [
    #         {
    #             "r": ...
    #         }
    #     ]
    # }
    #
    if isinstance(data, dict):

        rows = data.get("list")

        if isinstance(rows, list):

            for row in rows:

                sale = _extract_sale_from_row(row)

                if sale:
                    sales.append(sale)

    # Fallback recursive scan.
    if not sales:

        def walk(obj: Any):

            if isinstance(obj, dict):

                sale = _extract_sale_from_row(obj)

                if sale:
                    sales.append(sale)

                for value in obj.values():
                    walk(value)

            elif isinstance(obj, list):

                for value in obj:
                    walk(value)

        walk(data)

    # --------------------------------------------------------
    # Remove duplicate sale codes.
    # --------------------------------------------------------

    unique = {}

    for sale in sales:

        code = sale.get("code")

        if code:
            unique[code] = sale

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

    # TITLE
    bui.textwidget(
        parent=root,
        position=(0, 555),
        size=(780, 50),
        text="☠",
        scale=2.5,
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

    # CLOSE
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

    # ON / OFF
    toggle = bui.buttonwidget(
        parent=root,
        position=(65, 455),
        size=(650, 48),
        label="ON" if _enabled else "OFF",
        text_scale=1.1,
        color=(0.10, 0.65, 0.16)
        if _enabled else (0.55, 0.16, 0.16),
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

    # HEADERS
    bui.textwidget(
        parent=root,
        position=(70, 380),
        size=(280, 32),
        text="ITEM",
        scale=0.95,
        color=(1.0, 0.88, 0.20),
        h_align="center",
        v_align="center",
    )

    bui.textwidget(
        parent=root,
        position=(470, 380),
        size=(240, 32),
        text="MAXIMUM PRICE",
        scale=0.95,
        color=(1.0, 0.88, 0.20),
        h_align="center",
        v_align="center",
    )

    # ---------------------------------------------------------
    # 6 REAL EDITABLE ITEM + PRICE FIELDS
    # ---------------------------------------------------------

    row_y = 330
    row_gap = 52

    for i in range(MAX_SLOTS):

        # NUMBER
        bui.textwidget(
            parent=root,
            position=(20, row_y + 4),
            size=(35, 30),
            text=str(i + 1),
            scale=0.65,
            color=(0.65, 0.65, 0.70),
            h_align="center",
            v_align="center",
        )

        # ITEM FIELD BACKGROUND
        item_bg = bui.containerwidget(
            parent=root,
            position=(65, row_y - 2),
            size=(290, 40),
            background=True,
            color=(0.18, 0.18, 0.22),
            scale=1.0,
        )

        # REAL EDITABLE ITEM FIELD
        item_edit = bui.textwidget(
            parent=item_bg,
            position=(8, 2),
            size=(274, 36),
            editable=True,
            selectable=True,
            text=saved["items"][i],
            color=(0.95, 0.95, 0.98),
            v_align="center",
            h_align="center",
            padding=5,
        )

        _item_widgets.append(item_edit)

        # ARROW / PAIR INDICATOR
        bui.textwidget(
            parent=root,
            position=(370, row_y + 3),
            size=(80, 30),
            text="→",
            scale=1.0,
            color=(0.75, 0.75, 0.80),
            h_align="center",
            v_align="center",
        )

        # PRICE FIELD BACKGROUND
        price_bg = bui.containerwidget(
            parent=root,
            position=(465, row_y - 2),
            size=(250, 40),
            background=True,
            color=(0.18, 0.18, 0.22),
            scale=1.0,
        )

        # REAL EDITABLE PRICE FIELD
        price_edit = bui.textwidget(
            parent=price_bg,
            position=(8, 2),
            size=(234, 36),
            editable=True,
            selectable=True,
            text=saved["prices"][i],
            color=(0.95, 0.95, 0.98),
            v_align="center",
            h_align="center",
            padding=5,
        )

        _price_widgets.append(price_edit)

        row_y -= row_gap

    # ---------------------------------------------------------
    # SAVE / CLOSE
    # ---------------------------------------------------------

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
            position=(355, 335),
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



import base64
import json
import os
import re
import threading
import time
from typing import Any

import requests
from eth_account import Account
from eth_utils.crypto import keccak
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm.exact.register import register_exact_evm_client

BRAINCHAIN_URL = "https://brain.usebrainchain.com"

BRAINCHAIN_PAYMENTS = {
    "base": (
        "eip155:8453",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDC on Base",
    ),
    "robinhood": (
        "eip155:4663",
        "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "USDG on Robinhood Chain",
    ),
}

UNFUNDED = ("insufficient_funds", "insufficient_balance")

DEFAULT_LEVELS = [
    "Does everything the brief asks, correctly and completely",
    "Does what the brief asks, correctly, with at most minor gaps",
    "Misses part of what the brief asks, or gets something wrong",
]

POLL_SECONDS = 10

# How long a signed payment is valid when the offer names no limit.
PAYMENT_VALID_SECONDS = 300

NOT_TEXT = "title and brief must be text and levels a list of text, in valid UTF-8."

REFUND_NOTES = {
    "refunding": "The payment is being refunded to the paying wallet.",
    "refunded": "The payment was refunded to the paying wallet.",
    "refund-failed": "The refund failed: write to "
    "hello@usebrainchain.com with the order id.",
}

_orders: dict[tuple[str, bytes], dict[str, Any]] = {}
_task_locks: dict[tuple[str, bytes], threading.Lock] = {}
_registry_lock = threading.Lock()


def hire_on_brainchain(
    title: str,
    brief: str,
    levels: list[str] | None = None,
) -> str:
    """Hire a model on BrainChain for a task and return the accepted result.

    If the work is not accepted, the payment is refunded. Each
    task costs 1 USDC on Base or 1 USDG on Robinhood Chain. Briefs
    and accepted results are public, so never put secrets or private
    data in a brief.

    Args:
        title: One line naming the deliverable, up to 120 characters.
        brief: What to produce, with every fact the model needs, up to
            32,000 characters. The whole task must fit in 64,000 bytes
            as JSON. The hired model cannot browse.
        levels: Two or three quality levels, best first, each up to 200
            characters. With three, the work is accepted at the second
            level or better; with two, only at the first. Defaults to
            three general levels.

    Returns:
        A JSON string. When the work is accepted it holds accepted true
        and the result, or result_url when the text could not be read.
        When it is not, accepted false and the reason. When the outcome
        is not known yet, pending true: call again with the same
        arguments to keep waiting. A paid order is kept in this process,
        so an identical call never pays again. A refusal before payment
        holds error and charged false; an error without charged means the
        wallet may have been charged, so call again with the same
        arguments.
    """
    try:
        config = _config()
    except ValueError as error:
        return json.dumps({"error": str(error), "charged": False})

    if levels is None:
        levels = DEFAULT_LEVELS

    try:
        return json.dumps(_hire(config, title, brief, levels))
    except Exception as error:
        # The agent can act on a returned error; a raise may end its run.
        return json.dumps(
            {
                "error": str(error),
                "message": "The wallet may have been charged. Call again "
                "with the same arguments in this process: that never pays twice.",
            }
        )


def _hire(
    config: dict[str, Any], title: str, brief: str, levels: list[str]
) -> dict[str, Any]:
    task = {"title": title, "brief": brief, "levels": levels}
    try:
        # Raw UTF-8 keeps a non-English brief inside the byte limit.
        body = json.dumps(task, ensure_ascii=False).encode()
    except (TypeError, ValueError):
        return {"error": NOT_TEXT, "charged": False}

    key = (config["network"], body)

    with _registry_lock:
        task_lock = _task_locks.setdefault(key, threading.Lock())

    with task_lock:
        posted = _orders.get(key)
        if posted is None:
            posted = _post_task(config, body)
            if "order" not in posted:
                return posted
            _orders[key] = posted

    try:
        outcome = _wait_for_outcome(posted, config["wait_seconds"])
    except Exception as error:
        return {
            "pending": True,
            "error": str(error),
            "order": posted["order"],
            "status_url": f"{BRAINCHAIN_URL}/x402/tasks/{posted['order']}",
            "message": "The outcome could not be read. Call again "
            "with the same arguments in this process to keep waiting.",
        }

    if outcome.get("charged") is False:
        with task_lock:
            # A stale reader must not remove a replacement order.
            if _orders.get(key) is posted:
                _orders.pop(key)

    return outcome


def _config() -> dict[str, Any]:
    pay_on = _setting("BRAINCHAIN_PAY_ON", "base").lower()
    payer_key = _setting("BRAINCHAIN_PAYER_KEY", "")
    max_price = _setting("BRAINCHAIN_MAX_PRICE_UNITS", "1000000")
    wait_seconds = _setting("BRAINCHAIN_WAIT_SECONDS", "600")

    if pay_on not in BRAINCHAIN_PAYMENTS:
        raise ValueError("BRAINCHAIN_PAY_ON must be base or robinhood.")
    if not payer_key:
        raise ValueError("Set BRAINCHAIN_PAYER_KEY to the paying wallet's private key.")
    if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", payer_key):
        raise ValueError(
            "BRAINCHAIN_PAYER_KEY must be a 32-byte hex private key, with or without 0x."
        )
    if not max_price.isdigit() or int(max_price) <= 0:
        raise ValueError(
            "BRAINCHAIN_MAX_PRICE_UNITS must be a positive whole "
            "number of base units, such as 1000000 for 1 USDC."
        )
    if not wait_seconds.isdigit():
        raise ValueError(
            "BRAINCHAIN_WAIT_SECONDS must be a whole number of seconds, 0 or more."
        )

    network, asset, token = BRAINCHAIN_PAYMENTS[pay_on]

    return {
        "network": network,
        "asset": asset,
        "token": token,
        "payer_key": payer_key,
        "max_price": int(max_price),
        "wait_seconds": int(wait_seconds),
    }


def _setting(name: str, default: str) -> str:
    """Blank values count as unset."""
    return (os.getenv(name) or "").strip() or default


def _post_task(config: dict[str, Any], body: bytes) -> dict[str, Any]:
    url = f"{BRAINCHAIN_URL}/x402/tasks"

    try:
        challenge = _post(url, body, {}, timeout=30)
    except requests.RequestException as error:
        return {
            "error": "Could not reach BrainChain "
            f"({type(error).__name__}), so nothing was charged.",
            "charged": False,
        }

    if challenge.status_code != 402:
        return {"error": _error_text(challenge), "charged": False}

    try:
        offer = _offer(challenge, config)
        headers, order, payer = _sign_payment(challenge, config)
    except Exception as error:
        # Nothing has been sent yet.
        return {"error": str(error), "charged": False}

    valid_seconds = offer.get("maxTimeoutSeconds", PAYMENT_VALID_SECONDS)
    posted: dict[str, Any] = {
        "order": order,
        "expires_at": time.monotonic() + int(valid_seconds),
        "settled": False,
    }

    try:
        response = _post(url, body, headers, timeout=120)
    except Exception:
        return posted

    # 400 and 402 come before the payment is settled.
    if response.status_code in (400, 402):
        refusal = _error_text(response)
        if any(code in refusal for code in UNFUNDED):
            price = int(offer["amount"]) / 10**6
            refusal = (
                f"The paying wallet {payer} holds less than the "
                f"{price:g} {config['token']} this task costs."
            )
        return {"error": refusal, "charged": False}

    # BrainChain answers 202 only once the payment has settled.
    posted["settled"] = response.status_code == 202

    return posted


def _post(
    url: str, body: bytes, headers: dict[str, str], timeout: int
) -> requests.Response:
    return requests.post(
        url,
        data=body,
        headers={"content-type": "application/json", **headers},
        timeout=timeout,
        allow_redirects=False,
    )


def _sign_payment(
    challenge: requests.Response, config: dict[str, Any]
) -> tuple[dict[str, str], str, str]:
    try:
        account = Account.from_key(config["payer_key"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            "BRAINCHAIN_PAYER_KEY is not a valid wallet private key."
        ) from error
    client = x402ClientSync(lambda version, offers: _pick(offers))
    register_exact_evm_client(client, account, networks=config["network"])
    client.set_spend_controls(
        {
            "allowed_assets": [
                {
                    "network": config["network"],
                    "asset": config["asset"],
                    "max_amount_per_payment": str(config["max_price"]),
                }
            ]
        }
    )

    http_client = x402HTTPClientSync(client)
    required = http_client.get_payment_required_response(challenge.headers.get)
    headers = http_client.encode_payment_signature_header(
        client.create_payment_payload(required)
    )

    signed = json.loads(base64.b64decode(next(iter(headers.values()))))

    return headers, _order_id(signed["payload"]), account.address


def _order_id(payload: dict[str, Any]) -> str:
    """Name an order as BrainChain does: the hash of the signed payment."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    return "0x" + keccak(text=canonical).hex()


def _offer(challenge: requests.Response, config: dict[str, Any]) -> dict[str, Any]:
    """Pick the offer to pay and check its price against the cap."""
    try:
        required = json.loads(base64.b64decode(challenge.headers["PAYMENT-REQUIRED"]))
        offers = [
            offer
            for offer in required["accepts"]
            if offer["scheme"] == "exact"
            and offer["network"] == config["network"]
            and offer["asset"].lower() == config["asset"].lower()
            and int(offer["amount"]) > 0
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("BrainChain's payment request could not be read.") from error

    if not offers:
        raise ValueError(f"BrainChain does not take {config['token']} for tasks.")

    offer: dict[str, Any] = _pick(offers)

    if int(offer["amount"]) > config["max_price"]:
        raise ValueError(
            f"BrainChain asks {offer['amount']} base units of "
            f"{config['token']}, above BRAINCHAIN_MAX_PRICE_UNITS "
            f"({config['max_price']})."
        )

    return offer


def _pick(offers: list[Any]) -> Any:
    """Prefer an offer that needs no on-chain approval."""
    for offer in offers:
        extra = (
            offer.get("extra")
            if isinstance(offer, dict)
            else getattr(offer, "extra", None)
        )
        if (extra or {}).get("assetTransferMethod") != "permit2":
            return offer

    return offers[0]


def _wait_for_outcome(posted: dict[str, Any], wait_seconds: int) -> dict[str, Any]:
    status_url = f"{BRAINCHAIN_URL}/x402/tasks/{posted['order']}"
    reference = {"order": posted["order"], "status_url": status_url}
    deadline = time.monotonic() + wait_seconds

    while True:
        status, view = _read_order(status_url)
        outcome = _outcome(view) if status == 200 and view else None

        if outcome:
            return {**outcome, **reference}

        # BrainChain saves an order before settling it; a host's own 404 proves nothing.
        unrecorded = status == 404 and "error" in view and not posted["settled"]
        if unrecorded and time.monotonic() > posted["expires_at"]:
            return {
                "accepted": False,
                "charged": False,
                "reason": "BrainChain never recorded the payment and "
                "it has expired, so nothing was charged.",
                **reference,
            }

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            waiting_on = (
                "BrainChain is still working on the task."
                if status == 200 and view
                else "BrainChain has not confirmed the payment yet."
            )
            return {
                "pending": True,
                "message": f"{waiting_on} Call again with the same "
                "arguments to keep waiting.",
                **reference,
            }

        time.sleep(min(POLL_SECONDS, remaining))


def _outcome(view: dict[str, Any]) -> dict[str, Any] | None:
    state = view.get("state")
    task = view.get("task")
    if not isinstance(task, dict):
        task = {}

    if state == "failed":
        return {
            "accepted": False,
            "charged": False,
            "reason": "The payment did not settle, so nothing was "
            "charged and no task was posted.",
        }

    if view.get("result"):
        return _accepted(str(view["result"]), view.get("taskId"))

    if state in REFUND_NOTES or task.get("status") == "refunded":
        reason = view.get("refundReason") or "The work was not accepted."
        note = REFUND_NOTES.get(str(state), REFUND_NOTES["refunding"])
        return {"accepted": False, "reason": f"{reason} {note}"}

    if task.get("accepted") is False:
        return {
            "accepted": False,
            "reason": "The work was not accepted. The payment is "
            "refunded when the dispute window closes, unless a ruling "
            "overturns the grade.",
        }

    return None


def _accepted(result_url: str, task_id: Any) -> dict[str, Any]:
    reply = {"accepted": True, "task_id": task_id}

    # A result can link a worker's own URL; only BrainChain's is read.
    if result_url.startswith(f"{BRAINCHAIN_URL}/records/"):
        try:
            response = requests.get(result_url, timeout=30, allow_redirects=False)
            if response.status_code == 200:
                return {**reply, "result": response.text}
        except requests.RequestException:
            return {**reply, "result_url": result_url}

    return {**reply, "result_url": result_url}


def _read_order(url: str) -> tuple[int, dict[str, Any]]:
    try:
        response = requests.get(url, timeout=30, allow_redirects=False)
    except requests.RequestException:
        return 0, {}

    return response.status_code, _json_body(response)


def _error_text(response: requests.Response) -> str:
    error = _json_body(response).get("error") or response.text[:200]

    return f"BrainChain answered {response.status_code}: {error}"


def _json_body(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}

    return body if isinstance(body, dict) else {}

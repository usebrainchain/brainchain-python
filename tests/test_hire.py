import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest

from usebrainchain import hire as brainchain
from usebrainchain import hire_on_brainchain

URL = "https://brain.usebrainchain.com"
ORDER = "0x" + "ab" * 32
PAYER = "0x" + "33" * 20
RESULT_URL = f"{URL}/records/0x{'cd' * 32}.txt"
BRIEF = "List the prime numbers between 50 and 70, one per line."
SIGN_PAYMENT = brainchain._sign_payment

BASE = ("eip155:8453", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
ROBINHOOD = (
    "eip155:4663",
    "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
)

RUNNING = {"state": "posted", "task": {"status": "hired"}}
ACCEPTED = {
    "state": "posted",
    "taskId": "41",
    "task": {"status": "graded", "accepted": True},
    "result": RESULT_URL,
}
REJECTED = {
    "state": "posted",
    "task": {"status": "graded", "accepted": False},
}


class FakeResponse:
    def __init__(self, status_code, body=None, text="", headers=None):
        self.status_code = status_code
        self.body = body
        self.text = text or json.dumps(body)
        self.headers = headers or {}

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


def offer(chain, amount="1000000", valid_seconds=300, **extra):
    network, asset = chain

    return {
        "scheme": "exact",
        "network": network,
        "amount": amount,
        "asset": asset,
        "payTo": "0x" + "22" * 20,
        "maxTimeoutSeconds": valid_seconds,
        "extra": extra,
    }


def payment_required(*offers):
    required = {
        "x402Version": 2,
        "resource": {"url": f"{URL}/x402/tasks"},
        "accepts": list(offers),
    }
    encoded = base64.b64encode(json.dumps(required).encode()).decode()

    return FakeResponse(
        402,
        {"error": "Payment required"},
        headers={"PAYMENT-REQUIRED": encoded},
    )


@pytest.fixture
def desk(monkeypatch):
    state = SimpleNamespace(
        challenge=payment_required(offer(BASE, name="USD Coin", version="2")),
        paid=FakeResponse(202, {"order": ORDER}),
        views=[RUNNING],
        status=200,
        payments=0,
        gate=None,
        bodies=[],
        result=FakeResponse(200, text="53\n59\n61\n67"),
    )

    def post(url, data, headers, timeout=None, allow_redirects=None):
        assert allow_redirects is False
        state.bodies.append(data)
        if isinstance(state.challenge, Exception):
            raise state.challenge
        if "PAYMENT-SIGNATURE" not in headers:
            if state.gate:
                state.gate.wait()
            return state.challenge
        state.payments += 1
        if isinstance(state.paid, Exception):
            raise state.paid
        return state.paid

    def get(url, timeout=None, allow_redirects=None):
        assert allow_redirects is False
        if url == RESULT_URL:
            return state.result
        view = state.views.pop(0) if len(state.views) > 1 else state.views[0]
        return FakeResponse(state.status, view)

    monkeypatch.setenv("BRAINCHAIN_PAYER_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("BRAINCHAIN_PAY_ON", "base")
    monkeypatch.setenv("BRAINCHAIN_WAIT_SECONDS", "0")
    monkeypatch.delenv("BRAINCHAIN_MAX_PRICE_UNITS", raising=False)
    monkeypatch.setattr(brainchain, "_orders", {})
    monkeypatch.setattr(brainchain, "_task_locks", {})
    monkeypatch.setattr(
        brainchain,
        "_sign_payment",
        lambda challenge, config: (
            {"PAYMENT-SIGNATURE": "x"},
            ORDER,
            PAYER,
        ),
    )
    monkeypatch.setattr(brainchain.requests, "post", post)
    monkeypatch.setattr(brainchain.requests, "get", get)

    return state


def hire(brief=BRIEF):
    return json.loads(hire_on_brainchain("Primes", brief))


@pytest.mark.parametrize(
    "view, expected",
    [
        (
            ACCEPTED,
            {
                "accepted": True,
                "result": "53\n59\n61\n67",
                "task_id": "41",
            },
        ),
        (
            {**ACCEPTED, "result": "https://worker.example/out.txt"},
            {
                "accepted": True,
                "result_url": "https://worker.example/out.txt",
                "result": None,
            },
        ),
        (
            REJECTED,
            {
                "accepted": False,
                "reason": "The work was not accepted. The payment is "
                "refunded when the dispute window closes, unless a "
                "ruling overturns the grade.",
            },
        ),
        (
            {
                "state": "refunded",
                "refundReason": "No model was hired in time.",
            },
            {
                "accepted": False,
                "reason": "No model was hired in time. The full payment "
                "went back to the paying wallet.",
            },
        ),
        (
            {"state": "posted", "task": {"status": "refunded"}},
            {
                "accepted": False,
                "reason": "The work was not accepted. The payment is "
                "being refunded to the paying wallet.",
            },
        ),
        (
            {"state": "refund-failed", "task": "garbled"},
            {
                "accepted": False,
                "reason": "The work was not accepted. The refund "
                "failed: write to hello@usebrainchain.com with the "
                "order id.",
            },
        ),
        ({"state": "failed"}, {"accepted": False, "charged": False}),
    ],
)
def test_hire_returns_the_outcome_of_the_order(desk, view, expected):
    desk.views = [view]

    reply = hire()

    assert {key: reply.get(key) for key in expected} == expected
    assert reply["status_url"] == f"{URL}/x402/tasks/{ORDER}"
    assert desk.payments == 1


@pytest.mark.parametrize(
    "views, paid, first",
    [
        ([RUNNING, ACCEPTED], None, {"pending": True}),
        ([REJECTED], None, {"accepted": False}),
        ([{"state": "refunded"}], None, {"accepted": False}),
        ([{"state": "refund-failed"}], None, {"accepted": False}),
        (
            [RUNNING, ACCEPTED],
            FakeResponse(502, {"error": "Payment unconfirmed"}),
            {"pending": True, "order": ORDER},
        ),
        (
            [RUNNING, ACCEPTED],
            brainchain.requests.Timeout("slow"),
            {"pending": True, "order": ORDER},
        ),
        (
            [RUNNING, ACCEPTED],
            RuntimeError("boom"),
            {"pending": True, "order": ORDER},
        ),
    ],
)
def test_an_identical_call_never_pays_again(desk, views, paid, first):
    desk.views = views
    desk.paid = paid or desk.paid

    reply = hire()
    again = hire()

    assert {key: reply.get(key) for key in first} == first
    assert again.get("accepted") is (views[-1] is ACCEPTED)
    assert desk.payments == 1


def run_together(*briefs):
    replies = []
    calls = [
        threading.Thread(target=lambda b=brief: replies.append(hire(b)))
        for brief in briefs
    ]

    for call in calls:
        call.start()
    for call in calls:
        call.join()

    return replies


def test_identical_calls_at_once_pay_once(desk):
    desk.views = [ACCEPTED]
    desk.gate = SimpleNamespace(wait=lambda: time.sleep(0.1))

    replies = run_together(BRIEF, BRIEF)

    assert [reply.get("accepted") for reply in replies] == [
        True,
        True,
    ]
    assert desk.payments == 1


def test_different_tasks_post_at_the_same_time(desk):
    desk.views = [ACCEPTED]
    desk.gate = threading.Barrier(2, timeout=5)

    replies = run_together(BRIEF, "List the primes below 20.")

    assert [reply.get("accepted") for reply in replies] == [
        True,
        True,
    ]
    assert desk.payments == 2


def test_a_stale_failure_cannot_remove_a_new_paid_order(desk, monkeypatch):
    waiting = threading.Event()
    release = threading.Event()
    wait_for_outcome = brainchain._wait_for_outcome

    def wait(posted, wait_seconds):
        if threading.current_thread() is stale:
            waiting.set()
            assert release.wait(5)
            return {"accepted": False, "charged": False}
        return wait_for_outcome(posted, wait_seconds)

    monkeypatch.setattr(brainchain, "_wait_for_outcome", wait)
    stale = threading.Thread(target=hire)
    stale.start()
    try:
        assert waiting.wait(5)
        desk.views = [{"state": "failed"}]
        assert hire()["charged"] is False
        desk.views = [ACCEPTED]
        assert hire()["accepted"] is True
    finally:
        release.set()
        stale.join(timeout=5)

    assert not stale.is_alive()
    assert hire()["accepted"] is True
    assert desk.payments == 2


@pytest.mark.parametrize(
    "setup, error",
    [
        (
            lambda desk, env: env.delenv("BRAINCHAIN_PAYER_KEY"),
            "Set BRAINCHAIN_PAYER_KEY to the paying wallet's 32-byte hex private key.",
        ),
        (
            lambda desk, env: env.setenv("BRAINCHAIN_PAY_ON", "solana"),
            "BRAINCHAIN_PAY_ON must be base or robinhood.",
        ),
        (
            lambda desk, env: setattr(
                desk,
                "challenge",
                brainchain.requests.ConnectionError("down"),
            ),
            "Could not reach BrainChain (ConnectionError), so nothing was paid.",
        ),
        (
            lambda desk, env: setattr(
                desk,
                "challenge",
                FakeResponse(400, {"error": "levels must have 2 or 3"}),
            ),
            "BrainChain answered 400: levels must have 2 or 3",
        ),
        (
            lambda desk, env: setattr(
                desk,
                "challenge",
                payment_required(offer(BASE, "1000001")),
            ),
            "BrainChain asks 1000001 base units of USDC on Base, "
            "above BRAINCHAIN_MAX_PRICE_UNITS (1000000).",
        ),
        (
            lambda desk, env: setattr(
                desk,
                "paid",
                FakeResponse(402, {"error": "invalid_signature"}),
            ),
            "BrainChain answered 402: invalid_signature",
        ),
        (
            lambda desk, env: setattr(
                desk,
                "paid",
                FakeResponse(402, {"error": "insufficient_funds"}),
            ),
            f"The paying wallet {PAYER} holds less than the 1 USDC on "
            "Base this task costs.",
        ),
    ],
)
def test_refused_tasks_say_why_and_that_nothing_was_charged(
    desk, monkeypatch, setup, error
):
    setup(desk, monkeypatch)

    assert hire() == {"error": error, "charged": False}


def test_non_json_arguments_are_refused_before_payment(desk):
    reply = hire(object())

    assert "not JSON serializable" in reply["error"]
    assert reply["charged"] is False
    assert desk.bodies == []


def test_an_unrecorded_payment_is_dropped_only_once_it_expired(desk):
    desk.status = 404

    assert hire()["pending"] is True
    assert hire()["order"] == ORDER
    assert desk.payments == 1

    desk.challenge = payment_required(offer(BASE, valid_seconds=-1))
    reply = hire("List the primes below 20.")
    hire("List the primes below 20.")

    assert reply["charged"] is False
    assert desk.payments == 3


def test_a_redirected_result_is_not_read(desk):
    desk.views = [ACCEPTED]
    desk.result = FakeResponse(
        302, headers={"Location": "https://worker.example/out.txt"}
    )

    reply = hire()

    assert reply["accepted"] is True
    assert reply["result_url"] == RESULT_URL
    assert "result" not in reply


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_settings_use_defaults_and_briefs_post_as_utf8(desk, monkeypatch, empty):
    desk.views = [ACCEPTED]
    for name in ("PAY_ON", "MAX_PRICE_UNITS", "WAIT_SECONDS"):
        monkeypatch.setenv(f"BRAINCHAIN_{name}", empty)

    reply = hire("Résumé of 東京, in one line.")

    assert reply["accepted"] is True
    assert "東京".encode() in desk.bodies[0]


def test_a_poll_error_keeps_the_paid_order(desk, monkeypatch):
    def broken(url, timeout=None, allow_redirects=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(brainchain.requests, "get", broken)

    reply = hire()

    assert reply["error"] == "boom"
    assert reply["order"] == ORDER
    assert reply["status_url"] == f"{URL}/x402/tasks/{ORDER}"
    assert reply.get("charged") is not False
    assert hire()["order"] == ORDER
    assert desk.payments == 1


def test_the_order_id_matches_the_one_brainchain_computes():
    pytest.importorskip("eth_utils")
    payload = {
        "signature": "0x" + "ab" * 65,
        "authorization": {
            "to": "0x" + "22" * 20,
            "from": "0x" + "33" * 20,
            "value": "1000000",
            "validAfter": "0",
            "validBefore": "1795500000",
            "nonce": "0x" + "44" * 32,
        },
        "note": "café ✓",
    }

    # Computed with the desk's own orderId for this payload.
    assert brainchain._order_id(payload) == (
        "0xd17f9c7166da3ed2b502523d75d524b581a38b38215ebdb2b0a8bf4ace074605"
    )


def test_robinhood_payments_sign_without_an_approval(desk, monkeypatch):
    monkeypatch.setenv("BRAINCHAIN_PAY_ON", "robinhood")
    desk.challenge = payment_required(
        offer(ROBINHOOD, assetTransferMethod="permit2"),
        offer(ROBINHOOD, name="Global Dollar", version="1"),
    )
    config = brainchain._config()

    headers, order, payer = SIGN_PAYMENT(desk.challenge, config)
    signed = json.loads(base64.b64decode(next(iter(headers.values()))))

    assert "authorization" in signed["payload"]
    assert signed["accepted"]["extra"] == {
        "name": "Global Dollar",
        "version": "1",
    }
    assert order == brainchain._order_id(signed["payload"])


@pytest.mark.parametrize("payer_key", ["0" * 64, "f" * 64])
def test_invalid_wallet_keys_are_refused(desk, monkeypatch, payer_key):
    monkeypatch.setenv("BRAINCHAIN_PAYER_KEY", payer_key)
    monkeypatch.setattr(brainchain, "_sign_payment", SIGN_PAYMENT)

    assert hire() == {
        "error": "BRAINCHAIN_PAYER_KEY is not a valid wallet private key.",
        "charged": False,
    }
    assert desk.payments == 0

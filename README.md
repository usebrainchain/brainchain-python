# usebrainchain

Let a Python agent hire a model for a task and get the work back once it is accepted.

`hire_on_brainchain` is one function. The agent passes a title, a brief and the quality levels it expects. [BrainChain](https://usebrainchain.com) hires a model and grades the work against those levels. The function returns the result once it is accepted. If it is not, the payment is refunded to the paying wallet.

Each task costs 1 USDC on Base or 1 USDG on Robinhood Chain, paid from your own wallet over [x402](https://x402.org). You need no BrainChain account or API key.

## Install

```bash
pip install usebrainchain
```

Python 3.10 or later.

## Settings

```bash
# Private key of the wallet that pays. Use one that holds only what you plan to spend.
export BRAINCHAIN_PAYER_KEY="your-wallet-private-key"

# Optional, shown with their defaults.
export BRAINCHAIN_PAY_ON="base"              # or "robinhood"
export BRAINCHAIN_MAX_PRICE_UNITS="1000000"  # cap per task: 1 USDC or 1 USDG
export BRAINCHAIN_WAIT_SECONDS="600"         # how long one call polls for the result; 0 checks once
```

The wallet pays in USDC on Base (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) or in USDG on Robinhood Chain (`0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`). Each payment is a signature, so it needs no approval and no ETH.

The key never leaves your process: only signatures are sent.

The function never signs a payment above `BRAINCHAIN_MAX_PRICE_UNITS`, and it only pays on the chain `BRAINCHAIN_PAY_ON` names.

Each network request has its own timeout, so a call can take longer than `BRAINCHAIN_WAIT_SECONDS`.

## Use it as an agent tool

The function has typed arguments and a docstring, so a framework that builds tools from Python functions can take it as it is. This example uses [Swarms](https://github.com/kyegomez/swarms):

```python
from swarms import Agent
from usebrainchain import hire_on_brainchain

agent = Agent(
    agent_name="BrainChain-Hiring-Agent",
    model_name="gpt-5.4",
    system_prompt=(
        "You hand self-contained tasks to a hired model with "
        "hire_on_brainchain. Write a short title, a brief that holds "
        "every fact the model needs, and three quality levels, best "
        "first. If the tool returns pending, call it again with the "
        "same arguments; that does not pay again. Report the result, "
        "or the reason or error the tool returned."
    ),
    tools=[hire_on_brainchain],
    max_loops=3,
)

print(
    agent.run(
        "Hire a model on BrainChain to write a Python function "
        "is_valid_isbn10(text) that returns True only for a valid "
        "ISBN-10, allowing hyphens and a final X."
    )
)
```

The example's model needs its own key, here `OPENAI_API_KEY`. Each tool call waits up to `BRAINCHAIN_WAIT_SECONDS`, ten minutes by default, and returns `pending` after that.

## Call it directly

```python
import json
from usebrainchain import hire_on_brainchain

reply = json.loads(
    hire_on_brainchain(
        title="Primes between 50 and 70",
        brief="List every prime number between 50 and 70, one per line.",
        levels=[
            "All four primes, one per line, nothing else.",
            "All four primes, with extra text.",
            "A prime is missing or a number is wrong.",
        ],
    )
)

print(reply.get("result") or reply)
```

| Argument | Type | Description |
|---|---|---|
| `title` | `str` | One line naming the deliverable, up to 120 characters. |
| `brief` | `str` | What to produce, up to 32,000 characters. The hired model cannot browse, so put every fact it needs in the brief. |
| `levels` | `list[str]`, optional | Two or three quality levels, best first, each up to 200 characters. With three, the work is accepted at the second level or better; with two, only at the first. When omitted, three general levels are used. |

Character limits count UTF-16 units, so most emoji count as two. The whole task as JSON, title and levels included, must fit within 64,000 UTF-8 bytes.

## What it returns

A JSON string, on failure too: the function does not raise. Replies about a paid task carry its `order` id and a `status_url` that anyone can read.

| Reply | Meaning |
|---|---|
| `accepted: true` with `result` | The work was accepted. `result` is the text the hired model delivered and `task_id` the task's number. When the text could not be read, `result_url` links to it. |
| `accepted: false` with `reason` | The work was not accepted or the task could not run. `reason` says what happens to the payment. With `charged: false` the wallet was not charged. |
| `pending: true` | The outcome is not known yet: the task is still running, BrainChain has not confirmed the payment, or the task's status could not be read (then with `error`). Call again with the same arguments to keep waiting. |
| `error` with `charged: false` | The wallet was not charged and no task was posted, for example an invalid brief or too little in the wallet. |
| `error` without `charged` | Something failed unexpectedly and the wallet may have been charged. Call again with the same arguments in the same process: that never pays twice. |

The process remembers each paid task, so an identical call checks it and never pays again. A reply with `charged: false` can be retried once its cause is fixed. Restarting Python loses that memory, so the same call would pay again: follow the `status_url` instead. To hire again for the same task, change the brief.

Payments and refunds become final when the dispute window closes; the `status_url` shows when. An accepted result can be returned before then.

## Writing the brief

- Briefs and accepted results are public. Keep secrets and private data out of them.
- Put hard requirements, such as "three bullet points", in the brief and in the lowest level you accept.
- Describe quality levels in terms of the result. [The brief format](https://docs.usebrainchain.com/brief-format) has more examples.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy
```

The tests mock BrainChain's HTTP API and never pay.

## License

MIT

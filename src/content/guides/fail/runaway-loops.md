---
title: Runaway Loops and Rate-Limit Blowups
description: How agent loops run away, why retries multiply across layers, and the run budgets, jittered backoff, circuit breakers, and idempotency keys that stop them.
pillar: fail
updated: "2026-08-18"
keywords:
  - agent runaway loop
  - agent rate limit
  - agent cost blowup
  - exponential backoff full jitter
  - circuit breaker agent
  - idempotency key retry
  - agent run budget
relatedGuides:
  - connect/frameworks
  - connect/databases
  - govern/audit
howTo:
  - name: Measure a healthy run before setting any limit
    text: Record provider calls, tokens, cost, and duration across a few hundred normal runs, and read the percentiles rather than guessing a ceiling.
  - name: Put every provider call behind exactly one retry layer
    text: Delete the retries in your tool wrappers, your HTTP client, and your agent loop until only one of them retries, because layered retries multiply.
  - name: Back off with full jitter and honour Retry-After
    text: Sleep a random duration between zero and min(cap, base times two to the attempt), and never sleep less than the Retry-After header asked for.
  - name: Add a circuit breaker around each dependency
    text: Trip to open after a threshold of consecutive dependency failures, short-circuit while open, and admit a small number of trial calls after the reset timeout.
  - name: Give every write a stable idempotency key
    text: Derive the key from the operation and its parameters rather than generating a fresh one per attempt, and refuse to send a write that has no key.
  - name: Enforce a run budget over calls, tokens, cost, and wall clock
    text: Check every limit before each attempt, count retries against the same budget, and bound each request timeout by the time the run has left.
  - name: Wire a kill switch that does not require a deploy
    text: Read a stop flag keyed by agent and by user from outside the deploy artifact before every tool call, cached for a few seconds at most.
  - name: Run the broken loop and confirm the budget stops it
    text: Point the agent at a simulated provider, let the loop run away, and check that the run ends on your limit rather than on the provider's.
faq:
  - question: Why does my AI agent keep hitting rate limits?
    answer: Almost always because retries are happening at more than one layer. If a tool wrapper, an HTTP client, and the agent loop each retry three times, one logical call becomes twenty-seven requests at the provider. Collapse retries to a single layer, then add exponential backoff with full jitter on top.
  - question: What is exponential backoff with full jitter?
    answer: "It is the retry delay AWS describes in its article on exponential backoff and jitter: sleep for a random duration between zero and min(cap, base * 2 ** attempt), where attempt starts at zero for the first retry. The randomness is the point. Without it, every client that failed at the same moment retries at the same moment and rebuilds the burst that caused the failure."
  - question: How do I stop an AI agent from running up a large API bill?
    answer: Give every run a hard budget over four things at once - provider calls, tokens, spend in your own currency, and wall clock - and check all four before each attempt rather than after. Add a per-user daily spend cap enforced by reserving the money before the call, since a per-run cap does nothing about a thousand runs.
  - question: Do idempotency keys make it safe to retry an agent's writes?
    answer: Only if the key identifies the operation rather than the attempt. A key generated fresh on each retry deduplicates nothing. Derive it from the operation and its parameters, and check what your provider does with a reused key - Stripe, for example, stores the first response for 24 hours and replays it, and errors if the parameters differ.
  - question: When should an agent use a circuit breaker instead of just retrying?
    answer: Use both, for different failures. Backoff handles a request that failed once; a circuit breaker handles a dependency that is failing for everyone, by refusing to call it at all after a threshold of consecutive failures. Trip it only on timeouts, 429s, and 5xx responses, never on a 400 or a 401, which mean the agent's own request is wrong.
  - question: How fast should an agent kill switch take effect?
    answer: As fast as your cache TTL, and it must never require a deploy. Read a stop flag keyed by agent id and user id from a store outside the deploy artifact before every tool call, cache it for a few seconds, and accept that the TTL is your worst-case reaction time.
---

## TL;DR

- The symptom is a graph changing shape: provider calls per run, 429 rate, or spend. A staircase means retries multiplying across layers; a clean ramp means a loop with no terminating condition; a flat plateau at a new level means pagination that never ends.
- Retries multiply. Three layers that each retry three times send twenty-seven requests for one logical call, and the exercise in `examples/fail-runaway-loops/retry_amplification.py` prints exactly that number. Retry in one layer only.
- Back off with full jitter — `sleep = random_between(0, min(cap, base * 2 ** attempt))` — and treat `Retry-After` as a floor you never go below, with jitter added above it.
- The control that actually stops a runaway is a run budget over four quantities at once: provider calls, tokens, money, and wall clock. Derive each from a measured healthy run, not from taste.
- A circuit breaker stops you calling a dependency that is already down, and a stable idempotency key stops a retried write becoming two rows. Neither substitutes for the other, and neither substitutes for the budget.

## Who this is for

You run an agent that calls external APIs on a schedule or on user demand, and you have either just received a surprising invoice, a throttling alert, or an email from a partner about your request volume — or you would like to not receive one. Everything here is provider-neutral and runs offline, so you can reproduce each failure before it reproduces itself in production. Skip this if your agent makes a fixed, small number of calls per run with no retry logic and no write path: your blast radius is already bounded and the machinery below is overhead.

## The problem

Look at the graphs first, because the shape tells you which loop you have. There are five shapes worth being able to recognise on sight.

**1. The retry storm.** Call volume jumps by a multiple, holds, jumps again — a staircase. Each step is another layer of the stack entering its own retry loop, and the 429 rate climbs alongside it because every client retried at the same instant. Latency looks fine, because failing fast is fast. The mechanism is multiplication and it is covered in step 2.

**2. The recursive tool call.** The agent writes something, the write emits an event, the event wakes the agent, and the agent responds to itself. On a graph this is a clean upward ramp with no errors at all: the traffic is well-formed and well-paced, so no rate limiter objects. The tell is a write endpoint accumulating records that each look individually plausible, and a tool-calls-per-run ratio that climbs with no corresponding rise in runs.

**3. Two agents calling each other.** Agent A treats agent B as the authority on a question and B treats A the same way. Same ramp as shape 2, same absence of errors, but the records pile up in your own agent invocation table rather than in a provider's. Synchronous in-process calls eventually die on the interpreter's stack limit, which is why this one is often first seen in a stack trace rather than on a dashboard. Across a queue or an HTTP hop there is no stack, and therefore no limit.

**4. The planning loop that never converges.** The agent finishes a pass, cannot verify that it succeeded, and decides to check again. This shows up as duration rather than volume: runs that used to finish in nine seconds run until something else kills them, and the tool-call histogram grows a long right tail while the p50 does not move. The root cause is always the same — success is not expressible as a predicate a machine can evaluate — so the agent substitutes judgement, and judgement can always find another reason to look.

**5. Pagination that never terminates.** The flattest of the five: one endpoint, one parameter set, a constant rate, forever. The cursor is misread — either never sent back, or read from the presence of a key rather than its value — so page one is fetched indefinitely, or the walk restarts from the beginning each time it reaches the end. It is the loop most likely to survive a week unnoticed, because a steady, unremarkable request rate against a list endpoint looks exactly like a healthy integration.

And then there is the version with no graph at all. A write retried without an idempotency key commits twice. No error fires, no threshold is crossed, the run reports success. The duplicate is found a month later by whoever reconciles the ledger, and by then there are hundreds. In the demo below, the ungoverned agent settles 25 invoices and commits **160 charges** — 135 of them duplicates, every one of them a `200 OK`.

The bill is the loudest outcome and the least interesting one. The one that ends an integration is the partner who notices before you do. In the ungoverned run below, the simulated provider stops throttling after 60 rejections and disables the integration outright, which is a state no amount of client-side backoff recovers from. Getting it back is an email thread, not a code change.

What makes agents worse at this than ordinary software is that the call count is decided at runtime by a model. A hand-written integration that fetches 25 records fetches 25 records on every deploy. An agent decides how many times to look, and the decision is a function of a prompt, a tool description, and whatever the last tool call returned. Anything that can be influenced can be influenced into a loop, including by content the agent read from the system it is calling — the [failure pillar](/fail/) collects the neighbouring modes. So the ceiling cannot live in the agent's judgement. It has to live in the code around it.

## Step by step

The path below instruments one agent loop with a per-run budget, backoff with jitter, idempotency keys on writes, and a circuit breaker, then runs a deliberately broken agent through it and shows the budget stopping the run.

Everything is runnable offline. `examples/fail-runaway-loops/fake_provider.py` stands in for the API: it enforces a fixed-window rate limit, returns `429` with `Retry-After`, can be put into an outage, disables the integration after enough rejections, and implements idempotency server-side. A virtual clock replaces `time.sleep`, so a run that backs off for eighty seconds of simulated time finishes instantly and prints the same numbers on every machine.

### 1. Measure a healthy run before you set a single limit

A budget guessed from taste is either so loose it never fires or so tight it kills real work. Record four quantities per run — provider calls, tokens, cost, and duration — across a few hundred healthy runs, and read the percentiles.

`baseline()` in `examples/fail-runaway-loops/runaway_agent.py` does exactly that over 200 runs of a working agent against queues of 12 to 34 invoices:

```text
== baseline: 200 healthy runs, checkable done predicate ==
  provider calls per run  p50=52 p95=76 p99=76 max=76
  cost per run p99        $0.6840
  wall clock per run p99  9.12s
```

Then derive. The alert threshold goes at p99, so a regression pages someone. The hard budget goes at roughly twice p99, far enough above the alert that an unusually large queue is not killed and far enough below a runaway that the provider never notices:

```python
calls = int(math.ceil(measured["calls_p99"] * 2))
budget = RunBudget(
    max_provider_calls=calls,                                # 152
    max_tokens=COST.tokens * calls,                          # 136_800
    max_cost_usd=(COST.usd * calls).quantize(Decimal("0.0001")),  # $1.3680
    max_wall_clock_seconds=120.0,
)
```

Three of those four come off the measured distribution. Wall clock does not, and pretending otherwise is a mistake worth naming: a healthy run never backs off, so its duration tells you nothing about how long a legitimate run that hits throttling should be allowed to take. Set the wall-clock ceiling from how long the answer is still worth having, and treat it as the last wall rather than the first.

### 2. Collapse retries to one layer

Before adding anything, delete something. Count the places in your stack that retry: the SDK's built-in retry, your HTTP client's transport, the tool wrapper somebody added a decorator to, and the agent loop's own "if the tool failed, try again" branch. Each of them multiplies with the others.

The arithmetic is short enough to do in your head and worth running anyway. `examples/fail-runaway-loops/retry_amplification.py` builds the stack and counts requests at the bottom:

```text
one request into a three-layer stack, dependency down
  every layer retries 3x        -> 27 calls at the dependency
  only the layer above retries  -> 3 calls at the dependency
  four layers, all retrying 3x  -> 81 calls at the dependency
```

Twenty-seven and eighty-one are 3³ and 3⁴. That is the entire mechanism: retries compose by multiplication, so each additional retrying layer triples your traffic during an outage — exactly when the dependency has the least capacity to absorb it. Google's SRE book states the rule directly: "a failed request from the DB Frontend should only be retried by Backend B, the layer immediately above it. If multiple layers retried, we'd have a combinatorial explosion."

The same file also runs Google's second control, a per-client retry budget that only permits a retry while retries are under 10% of everything the client has sent:

```text
1000 requests from one client, dependency down, 3 attempts per request
  no per-client budget -> 3000 attempts (3.00x)
  10% retry budget     -> 1112 attempts (1.11x)
```

The SRE book gives "somewhere just below 3X" for the first case and "just 1.1x" for the second; this simulation makes every request fail, so it lands at exactly 3.00x and 1.11x. A per-client budget is worth having in a shared worker pool, where the per-run budget in step 6 bounds one run but not a thousand.

### 3. Back off with full jitter, and honour Retry-After

The formula comes from [AWS's article on exponential backoff and jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/), and the article's own [simulator source](https://github.com/aws-samples/aws-arch-backoff-simulator/blob/master/src/backoff_simulator.py) gives it unambiguously:

```python
def expo(self, n):
    return min(self.cap, pow(2, n) * self.base)

class ExpoBackoffFullJitter(Backoff):
    def backoff(self, n):
        v = self.expo(n)
        return random.uniform(0, v)
```

The article compares four strategies and is worth quoting precisely rather than paraphrasing, because the ranking is not "full jitter wins everything". Un-jittered exponential backoff is "the clear loser", taking both more work and more time. "Of the jittered approaches, 'Equal Jitter' is the loser. It does slightly more work than 'Full Jitter', and takes much longer." Between full and decorrelated jitter the call is closer: "The 'Full Jitter' approach uses less work, but slightly more time." Full jitter is the reasonable default; it is not free of trade-offs.

In `examples/fail-runaway-loops/guardrails.py`:

```python
_MAX_EXPONENT = 30


def full_jitter_delay(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    if base <= 0 or cap <= 0:
        raise ValueError("base and cap must be > 0")
    ceiling = min(cap, base * (2 ** min(attempt, _MAX_EXPONENT)))
    return rng.uniform(0.0, ceiling)
```

Three details in five lines. `attempt` is 0 for the first retry, so the first ceiling is `base`. The random window starts at **zero**, not at the previous ceiling — that is what decorrelates clients, and it is the difference between full jitter and the "exponential backoff" most people write, which doubles the delay but keeps every client that failed together waking together no matter how long the delays get. AWS's own description of what the randomness buys is the clearest one: "we want to spread out the spikes to an approximately constant rate". And the exponent is clamped, because `0.5 * 2 ** 1024` raises `OverflowError: int too large to convert to float`; nothing should ever retry a thousand times, but "should" is what this guide is about.

`Retry-After` is a different kind of instruction and deserves different handling. RFC 9110 §10.2.3 defines it as `HTTP-date / delay-seconds`, where `delay-seconds = 1*DIGIT` — so both `Retry-After: 120` and `Retry-After: Fri, 31 Dec 1999 23:59:59 GMT` are legal and your parser needs both. The RFC describes its use with 503 and with 3xx responses; the 429 status is defined separately in [RFC 6585 §4](https://www.rfc-editor.org/rfc/rfc6585.html#section-4), which says a 429 "MAY include a Retry-After header indicating how long to wait before making a new request". Many APIs send it on 429; not all do, so parse it when present and fall back to jittered backoff when it is absent.

When it is present, it is a floor:

```python
def next_delay(attempt, response, *, base, cap, rng, jitter_window=1.0):
    jittered = full_jitter_delay(attempt, base=base, cap=cap, rng=rng)
    if response is None:
        return jittered
    server_floor = parse_retry_after(response.headers.get("Retry-After"))
    if server_floor is None:
        return jittered
    return server_floor + rng.uniform(0.0, jitter_window)
```

Sleeping `random(0, 0.5)` when the server asked for 30 seconds is how an integration gets disabled. But every throttled client reading the same `Retry-After: 30` wakes at the same instant, which rebuilds the herd the jitter existed to break up. So honour the floor and scatter above it.

One parsing trap: `str.isdigit()` is the wrong test for `1*DIGIT`. It returns true for `"٣"` (U+0663), which `int()` then parses to 3 — a header you did not expect, silently accepted. The example matches `^[0-9]+$` instead, and the self-check asserts it.

### 4. Add a circuit breaker

Backoff handles a call that failed once. A breaker handles a dependency that is failing for everybody, by not calling it at all. The three states are [Martin Fowler's](https://martinfowler.com/bliki/CircuitBreaker.html), and his description of each is short enough to quote:

- **Closed.** Calls pass through. Consecutive failures are counted; a success resets the count to zero. "Once the failures reach a certain threshold, the circuit breaker trips."
- **Open.** "All further calls to the circuit breaker return with an error, without the protected call being made at all." After `reset_timeout` seconds, the next caller is promoted to half-open.
- **Half-open.** "The circuit is ready to make a real call as trial to see if the problem is fixed." A trial call "will either reset the breaker if successful or restart the timeout if not".

Thresholds worth starting from: `failure_threshold=5` consecutive dependency failures — the value in Fowler's own example — plus `reset_timeout=30s`, `half_open_max_calls=1`, and `success_threshold=2`. That last one is the single liberty this example takes with Fowler's description, which resets on one successful trial; requiring two stops the breaker flapping closed on the one request that happened to land on the healthy replica. Fowler also names the more sophisticated variant, "tripping once you get, say, a 50% failure rate", which is the right shape once your traffic is high enough that a consecutive-failure count is noisy.

Two decisions matter more than the numbers.

**What counts as a failure.** Only outcomes that mean *the dependency is sick*: timeouts, connection resets, `429`, and `5xx`. A `400` or `422` means the agent built a bad request; a `401` means the credential is wrong; a `403` means it lacks permission. Retrying any of those burns budget, and opening a breaker on them hides a code bug behind a plausible-looking outage. Fowler says the same thing about the pattern in general — "Not all errors should trip the circuit, some should reflect normal failures and be dealt with as part of regular logic" — and for an agent the list of normal failures is longer than usual, because the model generates the request. `is_dependency_failure()` in `guardrails.py` makes the split, and the self-check asserts that a 400 and a 401 do not trip it.

**Where the breaker lives.** One breaker per (dependency, credential) pair, shared across the worker pool if your architecture allows. Forty processes each holding a private in-memory breaker need forty times the failures before anything trips, which is a breaker that has never once fired.

What the agent does when the breaker is open is the part usually left out. It must not queue the work for later, because that is the same requests arriving in a burst when the timeout expires. It must not treat "breaker open" as a retryable error inside the same run — that is a retry loop wearing a breaker's clothes. The correct behaviour is to fail the step immediately, mark the run as blocked on that dependency, return whatever partial result is genuinely usable, and let the scheduler decide when to try the whole run again. Here is a run against a dependency that is down for 200 seconds while the agent polls every 5:

```text
== circuit breaker: dependency down for 200s, agent polls every 5s ==
  logical_calls: 60
  provider_calls: 33
  short_circuited: 36
  succeeded: 23
  breaker_state: closed
  transitions: ['open', 'half_open', 'open', 'half_open', 'open', 'half_open',
                'open', 'half_open', 'open', 'half_open', 'open', 'half_open', 'closed']
```

Sixty logical calls produced 33 requests instead of the 240 that four attempts each would have produced, the breaker probed once per reset timeout while the outage lasted, and it closed on its own when the dependency came back.

### 5. Give every write a stable idempotency key

An idempotency key is the control that makes retrying a write safe. It is worth being precise about the server-side semantics, because they are what you are relying on. Taking [Stripe's documented behaviour](https://docs.stripe.com/api/idempotent_requests) as the reference implementation:

- The key travels in the `Idempotency-Key` header and is **up to 255 characters**.
- The server stores the status code and body of the first request under a key, "regardless of whether it succeeds or fails", and replays that stored result to later requests carrying the same key.
- A replayed response is marked with the header `Idempotent-Replayed: true`.
- Reusing a key on "a request that does not match the first request's API endpoint and parameters" is an `idempotency_error` — the server refuses rather than silently applying the new parameters.
- A second request arriving while the first is still executing conflicts; Stripe's status table gives `409 Conflict` for "the request conflicts with another request (perhaps due to using the same idempotent key)".
- Keys are pruned after at least 24 hours, and a key reused after pruning "generates a new request".
- Keys belong on `POST`. Stripe says not to send them on `GET` and `DELETE`, which "are idempotent by definition" in its API.
- Rate limiting runs *before* the idempotency layer, so a `429` under a key is not stored and can produce a different result on the next attempt.

`examples/fail-runaway-loops/fake_provider.py` implements all of that server-side, which is what lets the client examples be tested rather than asserted.

The client-side rule that actually decides whether any of it helps: **the key must identify the operation, not the attempt.** This is the one people get wrong, and the wrong version looks completely correct in review — a `uuid.uuid4()` generated inside the retry loop is unique, random, and useless. Run `examples/fail-runaway-loops/duplicate_writes.py`, where every client sends the same charge, loses the first response in transit, and retries once:

```text
no idempotency key at all
  -> charges committed: 2

fresh uuid4 per attempt (the subtle one: a key that is never reused is not a key)
  -> charges committed: 2

stable operation key (acme:charge_invoice:85fdbdcb1033392359d908f31e9c5ee6)
  attempt 0: connection reset after POST /charges committed
  attempt 1: 200 (replayed) {'id': 'ch_0001', 'invoice': 'inv_2026_0042', 'amount_cents': 12900}
  -> charges committed: 1
```

Derive the key from a tenant, an operation name, and a hash of the parameters:

```python
def operation_key(*, tenant: str, operation: str, params: dict[str, Any]) -> str:
    fingerprint = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    key = f"{tenant}:{operation}:{fingerprint}"
    assert len(key) <= 255
    return key
```

Hashing the parameters gives a property a per-attempt key cannot: it also catches the *re-plan*. When an agent decides three steps later that it had better settle that invoice again, that is not a retry and no retry-scoped key would deduplicate it — but the operation is identical, so the key is identical, and the server replays instead of charging. Canonicalise the JSON with `sort_keys=True`, or two identical requests whose dicts iterate differently will produce two keys. Keep sensitive values out of the key: Stripe says to avoid using email addresses or personal identifiers, and the key ends up in logs and dashboards that the request body is usually scrubbed from.

Finally, make the unsafe path impossible rather than discouraged. The governed client in `guardrails.py` refuses to send a write without one:

```python
if method.upper() != "GET" and idempotency_key is None:
    raise ValueError(f"{method} {path} is a write and needs an idempotency_key")
```

### 6. Enforce the run budget

Four limits, checked before every attempt:

```python
@dataclass(frozen=True)
class RunBudget:
    max_provider_calls: int
    max_tokens: int
    max_cost_usd: Decimal
    max_wall_clock_seconds: float
```

Four properties of that check are load-bearing.

**Every attempt spends budget, including retries.** A budget that counts logical tool calls but not the retries underneath them does not bound the number of requests the provider sees, and the provider's number is the only one that matters to the provider.

**The check happens before the call, not after.** Checking afterwards is checking whether you have already overspent.

**Money is a first-class limit.** Calls and tokens are proxies; the invoice is denominated in currency, and a token budget set when your model cost one price does not move when the price does. `max_cost_usd` is the limit people add after the invoice, so add it before. Use `Decimal`, not `float`.

**The wall clock bounds the sleeps too, and each request.** A backoff that outlives the run is a run that never returns, so `check_sleep()` refuses a delay that would cross the deadline. And each request timeout is bounded by the time the run has left — a 30-second socket timeout inside a run with 4 seconds remaining overshoots the deadline by 26 seconds, once per attempt:

```python
timeout = min(self.request_timeout, max(0.0, self.ledger.seconds_left))
```

A per-run budget bounds one run. It does nothing about a thousand runs for one user, so add a per-user daily cap, and **reserve before the call rather than charging after**:

```python
def reserve(self, user_id: str, day: str, amount: Decimal) -> None:
    key = (user_id, day)
    current = self.spent.get(key, Decimal("0"))
    if current + amount > self.cap_usd:
        raise BudgetExhausted("daily_spend_usd", self.cap_usd, current + amount)
    self.spent[key] = current + amount
```

Charging after the response means N concurrent workers can each pass the check and each then spend, so the cap is exceeded by a factor of N. In production this is a row per `(user, day)` and an atomic `UPDATE ... SET reserved = reserved + $1 WHERE reserved + $1 <= cap` that returns zero rows when the cap is hit — see [connecting an agent to a database](/connect/databases/) for how to hold that kind of state without giving the agent a way around it.

Order of operations in the governed client is kill switch, then budget, then breaker, then the call. The kill switch is free and must win over everything; the budget is local and must be checked before anything is spent; the breaker decides whether the call is worth making; only then does anyone touch the network.

### 7. Wire the kill switch

The requirement is narrow and unforgiving: stop **one agent** for **one user**, right now, without a deploy. A deploy is a build, a rollout, and a worker restart — minutes at best, and the restart can re-queue exactly the work you were trying to stop.

So the flag lives outside the deploy artifact: a row, a feature-flag key, a control-plane endpoint. Not a constant, and not an environment variable baked into the image. It is read before every tool call and cached for a few seconds, and the cache TTL is your worst-case reaction time:

```python
def check(self, agent_id: str, user_id: str) -> None:
    state = self._load()
    if state.get("global"):
        raise Killed("kill switch: global stop")
    if agent_id in set(state.get("agents", [])):
        raise Killed(f"kill switch: agent {agent_id} stopped")
    pairs = {tuple(pair) for pair in state.get("pairs", []) if len(pair) == 2}
    if (agent_id, user_id) in pairs:
        raise Killed(f"kill switch: agent {agent_id} stopped for user {user_id}")
```

Three granularities, because during an incident you will want all three: `global` stops everything, `agents` stops one agent for everyone, and `pairs` stops one agent for one user and leaves the other nine hundred running. The demo flips the pair flag before call 12 of a run; the run stops at call 18, six calls later, when the cached flag expired — while the same agent keeps serving a different user:

```text
== kill switch: stop one agent for one user, mid-run ==
  outcome: kill switch: agent invoice-settler stopped for user user_42
  flag_set_before_call: 12
  calls_before_stop: 18
  cache_ttl_seconds: 1.0
  other_user_still_running: True
```

Decide what happens when the flag store is unreachable, and write the decision down. The example serves the last known value but not forever: if the last successful read is older than `max_staleness`, it stops the run. Failing closed costs availability during a control-plane outage. Failing open means an agent nobody can stop keeps running, which is the incident this whole guide is about.

### 8. Run the loop and watch the budget stop it

Now the demonstration. `settle_queue()` in `runaway_agent.py` walks a queue of invoices and settles each one, then asks whether it is done. With `checkable_done=False` the planner has no machine-checkable predicate, so "verify once more" always wins and the whole pass repeats — shape 4 from the problem section, wrapped in shape 1. **The agent function is identical in both runs below.** Only the harness differs.

First, ungoverned: a client with two nested retry loops of three attempts each, flat one-second backoff, no jitter, no budget, no breaker, and no idempotency key on writes.

```text
== ungoverned: same broken planner, naive retries, no budget ==
  outcome: provider disabled the integration: this integration has been disabled for abuse
  provider_calls: 241
  rate_limited: 60
  forbidden: 1
  charges_committed: 160
  distinct_invoices: 25
  elapsed: 88.92
  cost_usd: 2.169
```

The run did not end because the agent finished or because anything in your code noticed. It ended because the provider revoked the integration. Along the way it committed 160 charges against 25 distinct invoices.

Now the same broken agent inside the budget from step 1:

```text
== governed: same broken planner, inside the budget ==
  outcome: budget exhausted: provider_calls limit 152, would reach 153
  provider_calls: 152
  rate_limited: 7
  disabled: False
  charges_committed: 25
  distinct_invoices: 25
  idempotent_replays: 104
  elapsed: 76.88
  cost_usd: 1.368
  stopped_by: provider_calls
```

The loop is still broken. Nothing here fixed the planner. What changed is everything downstream of it. The run stopped on **your** limit rather than the provider's, at 152 calls instead of 241. The share of calls that were throttled fell from 60 of 241 to 7 of 152 — 25% to 5% — because backoff waited out the rate-limit window instead of hammering it, so the drop is not just a consequence of making fewer calls. The integration is intact. And the 104 repeated writes were replayed by the idempotency layer instead of committing, so the ledger holds 25 charges rather than 160. `stopped_by: provider_calls` is the field to emit as a metric: it names which wall you hit, which is the first question during triage.

To see the other shapes stopped the same way, `examples/fail-runaway-loops/pagination_loop.py` runs two real cursor bugs (a loop that never sends the cursor, and `if "next_cursor" in body` where the key is present with a null value) against a guarded walk, and `examples/fail-runaway-loops/agent_cycle.py` runs two agents that defer to each other and a webhook echo where the agent's own comment wakes the agent.

## Decision table

| Option | When it wins | Blast radius | Per-user fairness | What it costs |
| --- | --- | --- | --- | --- |
| Client-side backoff only | One agent, one process, one credential, low volume. The minimum that is not negligent. | One process. Nothing stops N replicas summing to N times the traffic, and nothing stops a run that never converges. | None. One user's runaway consumes the shared quota and everyone else gets the 429s. | An afternoon. No new infrastructure, no new failure mode. |
| Backoff plus circuit breaker and a run budget | The default for a single service that owns its credential. Everything in the step-by-step above. | One run, bounded on four axes. Still per-process unless the breaker and the daily ledger are shared. | Only if the daily spend ledger is shared storage. In-memory, it is per-replica and the cap is really N times the cap. | A few days, plus a shared store for the ledger and the kill-switch flag, plus the discipline to keep retries in one layer. |
| A broker enforcing quotas centrally | Many agents, many users, more than one team, or a credential shared across services. Also the only option that makes "stop this agent for this user" a single operation. | The fleet. A quota at the routing point bounds every caller, including the replica somebody deployed without telling you. | Native, because the broker sees the user on every call and can price and cap per user. | A dependency in the request path, its own availability budget, and a policy model to maintain. The cost is up front, not in the incident. |

The honest comparison is that the middle row is where most teams should be, and the third row is what the middle row turns into once there are two services and a shared credential. The failure that pushes people over is always the same: a per-process budget looks like a fleet budget until the day someone scales the deployment to forty replicas and the ceiling silently multiplies by forty.

## Checklist

- [ ] Exactly one layer in the call path retries. You have grepped for retry decorators, SDK retry configuration, and `for attempt in range(...)` and can name the single survivor.
- [ ] Backoff is `random_between(0, min(cap, base * 2 ** attempt))`, and a test asserts the delay never exceeds the ceiling and never goes below zero.
- [ ] `Retry-After` is parsed for both RFC 9110 forms — `delay-seconds` and `HTTP-date` — with a `^[0-9]+$` test rather than `str.isdigit()`, and is treated as a floor with jitter added above it.
- [ ] The retry classifier retries `408`, `429`, `5xx`, and transport errors, and nothing else. A `400`, `401`, or `403` is surfaced immediately.
- [ ] A circuit breaker wraps each dependency, trips on consecutive dependency failures only, and is shared across the worker pool rather than held per process.
- [ ] When the breaker is open the agent fails the step, does not queue the work, and does not treat it as retryable inside the same run.
- [ ] Every write carries an idempotency key derived from the operation and a canonical hash of its parameters, never generated inside the retry loop.
- [ ] The client raises rather than sending a write with no idempotency key.
- [ ] You have confirmed against your provider's own documentation how long it stores idempotency results and what it does when a key is reused with different parameters.
- [ ] Each run has a hard limit on provider calls, tokens, spend, and wall clock, and every retry counts against all four.
- [ ] Budget checks run before each attempt, and the per-request timeout is bounded by the run's remaining time.
- [ ] A per-user daily spend cap is reserved before the call and reconciled after, in shared storage rather than process memory.
- [ ] Budget numbers were derived from a recorded distribution of healthy runs, and you can name the p99 they came from.
- [ ] Alerts exist on tool calls per run, 429 rate, and cost per run, each with a threshold traceable to that baseline.
- [ ] Metrics carry `run_id`, `agent_id`, `user_id`, and `tool` labels, so an alert can name the user to stop.
- [ ] A kill switch stops one agent for one user without a deploy, and you have tested it in production this quarter.
- [ ] Pagination loops stop on the cursor's value, on a repeated cursor, on an empty page, and on a hard page cap.
- [ ] Agent-to-agent calls carry a call path and a depth limit, and an agent refuses a call whose path already contains it.
- [ ] Every event the agent emits is tagged with the agent's own actor id, and event handlers ignore events they caused.

## Failure modes

### The invoice arrives before the alert

**Symptom:** nothing pages. Runs succeed, latency is normal, error rates are flat. The first signal is an invoice materially larger than last month's, or a spend alert that arrives days after the spend it is alerting on.

**Cause:** every metric you alert on is a rate, and a runaway inside a normal-looking run does not change any rate. Ten times the tool calls at the same requests-per-second, spread over ten times the duration, looks identical on a per-second graph.

**Fix:** alert on per-run aggregates, not per-second rates. Tool calls per run, tokens per run, and cost per run, each with a p99 threshold taken from the baseline distribution — 76 calls per run in the example above, so the alert fires at 76 and the budget stops the run at 152. Add a cumulative daily spend alert per user at 80% of the hard cap, so the wall is visible before you hit it. And emit `stopped_by` on every terminated run: a rising count of `stopped_by: provider_calls` is a runaway that the budget is already absorbing, which is a bug report, not an incident.

### The 429 storm that will not clear

**Symptom:** the 429 rate climbs, your retry logic engages, and the 429 rate climbs further. Throughput collapses even though the provider is healthy, and it stays collapsed after you reduce concurrency.

**Cause:** two mechanisms, usually together. Retries at multiple layers multiply the load by 27 or 81 as measured above. And retries without jitter synchronise: every client that failed in the same second retries in the same second, and doubling the delay does not help because they all double it identically. Honouring `Retry-After` without adding jitter above it makes this worse, not better — the server has now told every client the exact same wake-up time.

**Fix:** one retry layer; full jitter; `Retry-After` as a floor with a random offset above it. Then verify with the number that matters, which is requests observed at the provider rather than requests you think you sent. Track it as a share of calls rather than a count, or a drop in traffic will look like a fix — the throttled share in step 8 fell from 25% to 5% with no change to the agent at all.

### Duplicate records nobody notices for a month

**Symptom:** no symptom. No error, no alert, no latency change. Eventually somebody reconciling a ledger finds two of something, and then finds hundreds.

**Cause:** a write retried without a stable idempotency key. The commonest version is a response lost in transit *after* the server committed: the client cannot distinguish that from a request that never arrived, so it retries, and the server has no way to know it is the same operation. The subtler version is a key that exists but is generated fresh on each attempt.

**Fix:** derive keys from the operation, refuse to send a keyless write, and reconcile. Add a uniqueness constraint on the business identifier downstream where you own the schema, because an idempotency key protects the retry window and a constraint protects everything else. Note that Stripe prunes keys after 24 hours and a key reused after pruning generates a new request — a weekly job replaying last week's work with last week's keys gets no protection at all.

### The breaker that never trips, or trips on the wrong thing

**Symptom:** either a dependency is down for twenty minutes and the breaker in your dashboard has never left closed, or the breaker is open and the dependency is provably healthy.

**Cause:** scope, or classification. A breaker held in process memory across 40 replicas needs 40 × `failure_threshold` failures before anything happens fleet-wide, so it effectively never fires. Classification is the other half: counting a `400` or `401` as a dependency failure opens the breaker on a bug in the agent's own request building, which then presents as an outage nobody can find.

**Fix:** one breaker per (dependency, credential), shared across the pool. Trip only on transport errors, `408`, `429`, and `5xx`, and assert that in a test — the self-check in `guardrails.py` asserts that a 400 and a 401 do not count. Export the state as a metric so "the breaker is open" is something you can see rather than infer.

### Pagination that runs forever

**Symptom:** one endpoint, one parameter set, a constant request rate, indefinitely. The agent never produces a result, or produces one with wildly duplicated rows.

**Cause:** the cursor is misread. Two shapes cover most of it. First, trusting a `has_more` flag while never sending the cursor back, so page one is fetched forever. Second, `if "next_cursor" in body` when the last page carries `"next_cursor": null` — a present key with a null value, so the loop continues, sends `cursor=null`, and the provider reads that as "start from the beginning". Both are in `examples/fail-runaway-loops/pagination_loop.py`, where the run budget is what stops them at 40 pages.

**Fix:** four guards, and use all four, because the first three are beliefs about the provider's behaviour and the fourth is not. Stop on the cursor's *value* rather than its key's presence. Stop if the server returns a cursor you already sent. Stop if a page returns no rows, because no progress is a stop condition. And cap the page count anyway, well above the largest legitimate result set. The guarded walk in the same file returns 25 items in 3 pages.

### Two agents that defer to each other

**Symptom:** a clean upward ramp in agent invocations with no errors at all. Traffic is well-formed and well-paced, so no rate limiter objects.

**Cause:** agent A treats agent B as the authority on a question, and B treats A the same way. Nothing in either agent is wrong on its own. Synchronous in-process calls end in a `RecursionError` — in `examples/fail-runaway-loops/agent_cycle.py` the unguarded version stops after 499 invocations, but that number is a property of the interpreter's recursion limit and not of your system. Across a queue or an HTTP hop there is no stack and therefore no limit at all.

**Fix:** carry a call context with every agent-to-agent invocation, holding the chain of agent ids that led here, a maximum depth, and the root run id. Refuse the call if your own id is already on the path — checking only the immediate caller misses a three-agent cycle, so check the whole path. Keep the depth ceiling as well, because it catches the case the path check cannot: a chain of distinct agents that never repeats and never ends. Guarded, the same demo stops after 3 invocations with `CycleDetected: billing -> support -> billing`. The root run id is also what lets an audit trail tie the whole call tree back to one user action; the [governance pillar](/govern/) covers that attribution problem in general.

### The agent triggers itself through its own webhook

**Symptom:** a ticket, document, or channel filling with near-identical agent messages, each one a valid response to the last.

**Cause:** the agent writes a comment, the system emits `comment.created`, the event handler wakes the agent, and the agent responds to its own comment. Filtering on the bot's display name works until somebody renames the bot.

**Fix:** tag every write with the agent's own actor identifier — a stable id, not a display name — and have the handler drop events carrying it. Propagate a causation id through the chain so you can see what triggered what. In `agent_cycle.py`, untagged writes hit the 500-write hard cap still looping; tagged writes drain after 2.

### The kill switch that needs a deploy

**Symptom:** an incident where the fix is "stop the agent" and the time-to-mitigate is measured in the length of your CI pipeline. Or worse: the deploy restarts the workers, the queue redelivers, and the loop resumes on the new build.

**Cause:** the stop condition lives in code, in an environment variable, or in a config file baked into the image.

**Fix:** the flag lives in a store you can write during an incident, is read before every tool call with a short cache TTL, and supports three granularities so you are not choosing between "do nothing" and "stop the product". Test it on a real agent in production on a schedule, because an untested kill switch is a comment.

## Doing this at scale

Everything above bounds one process. That is the ceiling on the do-it-yourself version, and it is worth being blunt about where it stops.

The run budget in `guardrails.py` is a Python object. Scale the deployment to forty replicas and you have forty budgets, and the fleet's actual ceiling is forty times what you configured — silently, on a routine capacity change nobody reviewed as a policy decision. The circuit breaker has the same problem in reverse: forty private breakers need forty times the failures before anything trips. The daily spend ledger has to become a row with an atomic conditional update, and something has to own that row. The kill switch needs a store, a write path an on-call engineer can reach at 3am, and a granularity model. And every one of those has to be reimplemented when the second team ships the second agent against the same credential, or the ceilings do not compose.

The pattern that fixes it is not a better library. It is moving enforcement to the point where the calls actually route. If every agent reaches the provider through one path, the quota is a single counter rather than forty, per-user fairness is available because the caller's identity is on every request, and stopping one agent for one user is one write to one place rather than a deploy to every service that might be involved.

That routing point is what [Agentic Fabriq](https://agenticfabriq.com) is: agents call named connections through the control layer instead of holding credentials and calling providers directly, policy is evaluated per request, and every call is attributed to an agent and the user it acted for. The consequence that matters here follows from the architecture rather than from any particular feature: because the agent never holds the provider credential, a call the layer declines is a call that does not reach the provider — including from the replica somebody scaled up last week and nobody is tracking. That is the property a per-process budget can never have. What your own gateway exposes on top of it — per-agent quotas, per-user quotas, a stop flag, and their exact semantics — is deployment configuration, so read it off your dashboard and `afctl agents list` rather than assuming it from this page.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        agents = await af.list_agents()
        print(f"agents registered with the layer: {agents}")

        tools = await af.list_tools()
        print(f"tools this token can reach: {tools}")

        result = await af.invoke_connection(
            "billing_prod",
            method="create_charge",
            parameters={
                "invoice": "inv_2026_0042",
                "amount_cents": 12_900,
                "idempotency_key": "invoice-settler:user_42:settle:85fdbdcb1033",
            },
        )
        print(result)


asyncio.run(main())
```

The runnable version is `examples/fail-runaway-loops/fabriq_quotas.py`. Two things it deliberately does not do. It does not drop the idempotency key: routing a write through a layer does not make the write idempotent, the key still has to identify the operation rather than the attempt, and it still has to reach the downstream provider. And it does not assume the response shape — connection names, method names, quota rejection payloads, and result keys are per-deployment, so run `afctl tools list` against your own gateway and branch on what it prints rather than on `billing_prod` or `create_charge`.

The parts that stay yours either way are the parts a routing layer cannot see: whether your planner has a checkable success predicate, whether your pagination stops on the cursor's value, and whether two of your agents defer to each other. A quota bounds the damage from those bugs. It does not fix them. What a layer like [Agentic Fabriq](https://agenticfabriq.com) removes is the fleet-arithmetic problem — one counter instead of forty, one kill switch instead of a deploy — and everything in the step-by-step remains correct if you would rather run those counters yourself.

## Further reading

The [connect pillar](/connect/) covers the integrations these loops run away against, and [wiring agents through a framework](/connect/frameworks/) is where the duplicated retry layer usually hides: frameworks retry, their HTTP clients retry, and the tool you wrapped retries. If your budgets and spend ledgers need to live in shared storage rather than process memory, [connecting an agent to a database](/connect/databases/) covers holding that state without handing the agent a way around it. The [govern pillar](/govern/) covers the attribution side — a kill switch keyed by agent and user is only usable if you know which agent acted for which user.

Primary sources for everything asserted above:

- [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) — AWS's comparison of un-jittered exponential backoff, Full Jitter, Equal Jitter, and Decorrelated Jitter, and what each costs in work and in time.
- [aws-arch-backoff-simulator](https://github.com/aws-samples/aws-arch-backoff-simulator/blob/master/src/backoff_simulator.py) — the simulator that produced the article's graphs, and the unambiguous source for the four formulas. The article renders them as images; this file does not.
- [RFC 9110 §10.2.3, Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after) — the `HTTP-date / delay-seconds` grammar, `delay-seconds = 1*DIGIT`, and its documented use with 503 and 3xx responses.
- [RFC 6585 §4, 429 Too Many Requests](https://www.rfc-editor.org/rfc/rfc6585.html#section-4) — the 429 status and the note that a response MAY include `Retry-After`.
- [Stripe: idempotent requests](https://docs.stripe.com/api/idempotent_requests) — the `Idempotency-Key` header, the 255-character limit, the stored-and-replayed first response, the 24-hour pruning window, and the rule that `GET` and `DELETE` do not take keys.
- [Stripe: advanced error handling](https://docs.stripe.com/error-low-level) — `Idempotent-Replayed: true`, `409 Conflict` for a concurrent duplicate, and the note that rate limiters run before the idempotency layer.
- [Stripe: errors](https://docs.stripe.com/api/errors) — the `idempotency_error` type, returned when a key is reused on a request that does not match the first request's endpoint and parameters.
- [Martin Fowler: CircuitBreaker](https://martinfowler.com/bliki/CircuitBreaker.html) — the closed, open, and half-open states, the trial call in half-open, a failure threshold of 5 in the worked example, and the rule that not all errors should trip the circuit.
- [Google SRE Book, Handling Overload](https://sre.google/sre-book/handling-overload/) — the per-request retry budget of three attempts, the 10% per-client retry budget, and the combinatorial explosion from retrying at more than one layer.

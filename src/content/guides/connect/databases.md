---
title: Letting an Agent Query Your Database Safely
description: Give an AI agent scoped PostgreSQL access with a dedicated role, row-level security keyed to the acting user, statement timeouts, and tools instead of raw SQL.
pillar: connect
updated: "2026-08-18"
keywords:
  - ai agent database access
  - llm sql injection
  - row level security agent
  - text to sql risk
  - postgres statement timeout
  - agent least privilege database
relatedGuides:
  - connect/github
  - govern/least-privilege
  - fail/runaway-loops
howTo:
  - name: Create a login role that belongs to the agent alone
    text: Create the role with NOSUPERUSER, NOCREATEDB, NOCREATEROLE and NOBYPASSRLS, then revoke the defaults that PUBLIC holds on the database and the public schema.
  - name: Grant SELECT on named relations only
    text: Grant USAGE on the schema and SELECT on each table the agent actually needs, using column lists where a table has columns it should never read.
  - name: Attach resource ceilings to the role
    text: Use ALTER ROLE to set statement_timeout, lock_timeout, idle_in_transaction_session_timeout and default_transaction_read_only so every session starts bounded.
  - name: Point the agent at a read replica
    text: Send agent traffic to a hot standby, where every transaction is read-only at the server level and a runaway query cannot compete with production writes.
  - name: Carry the acting user's identity into the session
    text: Open a transaction and call set_config('app.acting_user_id', $1, true) so the identity is a bound parameter and expires with the transaction.
  - name: Enable and force row-level security
    text: Run ALTER TABLE ... ENABLE ROW LEVEL SECURITY and ALTER TABLE ... FORCE ROW LEVEL SECURITY, create a permissive policy for the table's owning role and one for the agent that reads the session variable, and keep the owning role NOLOGIN.
  - name: Expose parameterized tools instead of a SQL box
    text: Ship a fixed set of functions with bound parameters and validated arguments, and reserve generated SQL for the cases where the question space is genuinely open.
  - name: Log the query and the user who caused it
    text: Set application_name per run and turn on statement logging on the agent's role, so every statement in the server log ties back to a request and a person.
faq:
  - question: Is a read-only database user enough to make an AI agent safe?
    answer: No. A read-only user still reads every row of every table it was granted, and a single generated cross join can exhaust the database's CPU or memory. Read-only limits which verbs run, not how much data is exposed or how much load a query creates. You also need row-level security, a statement timeout, and grants on named relations only.
  - question: How do you pass the end user's identity into a PostgreSQL row-level security policy?
    answer: Call SELECT set_config('app.acting_user_id', $1, true) at the start of the transaction and read it in the policy as NULLIF(current_setting('app.acting_user_id', true), '')::bigint. The two-argument form of current_setting exists in PostgreSQL 9.6 and later. Do not use SET, which cannot take a bound parameter and therefore forces string concatenation.
  - question: Does using parameterized queries protect a text-to-SQL agent from prompt injection?
    answer: No. Placeholders stop a value from being reinterpreted as SQL syntax by the database driver. In a text-to-SQL agent the attacker's text reaches the model, and the model writes a new query that is perfectly parameterized and syntactically valid. The injection happens upstream of the placeholder, so the placeholder never sees it.
  - question: When is text-to-SQL worth the risk for an AI agent?
    answer: When the question space is genuinely open-ended, the target is an analytics replica rather than the transactional primary, row-level security keyed to the acting user is already in place, and a human reads the result before anything acts on it. For a product feature with a countable set of questions, a fixed set of parameterized tools is both safer and more accurate.
  - question: Why does my row-level security policy still show every row to the agent?
    answer: Most often the query goes through a view. A view resolves its base tables with the privileges and policies of the view owner unless it was created WITH (security_invoker = true), which PostgreSQL 15 added. The other common cause is that the agent connects as the table owner, who is exempt from policies unless the table has ALTER TABLE ... FORCE ROW LEVEL SECURITY.
  - question: Why does my application read zero rows after enabling FORCE ROW LEVEL SECURITY?
    answer: Because FORCE subjects the table's owner to its policies too, and a table with FORCE and no policy matching the owner default-denies against it. The owning role reads nothing and every write fails with "new row violates row-level security policy". Add a policy for the owner in the same migration, such as CREATE POLICY app_owner_all ON public.orders FOR ALL TO app_owner USING (true) WITH CHECK (true). This never reproduces when you test as a superuser, because superusers bypass row-level security regardless of FORCE.
  - question: What does FORCE ROW LEVEL SECURITY actually protect against?
    answer: Less than it appears. FORCE changes the behaviour of one role, the table's owner, and only to the extent that the owner's own policy is narrow. Since the owner needs a permissive policy for migrations to work, and can in any case run ALTER TABLE ... DISABLE ROW LEVEL SECURITY and DROP POLICY as ownership rights no policy can revoke, FORCE does not contain the owner. What it buys is that the owner's access becomes an explicit predicate you can read and narrow instead of a silent exemption. The agent is contained by ENABLE plus its own policy, not by FORCE. Keep the owning role NOLOGIN and connect the application as a separate non-owner role.
  - question: Can the agent turn off the statement timeout you set with ALTER ROLE?
    answer: Yes. statement_timeout has a context of user in pg_settings, so any session can raise it or set it to zero. ALTER ROLE gives you a safe default, not an enforced ceiling. Enforce the real limit outside the session, at the connection pooler or proxy, and make privileges rather than timeouts the thing the agent cannot change.
---

## TL;DR

- A read-only role is not containment. It reads every row of every table you granted it, and one generated cross join can saturate the server. Read-only limits verbs, not blast radius.
- The layers that hold, in order: a login role that belongs to the agent alone, a read replica, `GRANT SELECT` on named relations, `ALTER ROLE ... SET statement_timeout`, row-level security keyed to the acting user, and a fixed set of parameterized tools. Watch one edge on the way in: `FORCE ROW LEVEL SECURITY` applies to the table's owner too, so forcing a table without also giving its owning role a policy default-denies against your own application — reads empty, writes rejected — and testing as a superuser will not show you. Give the owner a permissive policy, then keep the owner `NOLOGIN`, because that policy is what stops `FORCE` containing it.
- Carry the end user's identity with `SELECT set_config('app.acting_user_id', $1, true)` and read it in policies as `NULLIF(current_setting('app.acting_user_id', true), '')::bigint`. `SET` cannot take a bound parameter, which is why the obvious alternative is string concatenation.
- Parameterized queries do not stop prompt injection in a text-to-SQL agent. Placeholders keep a value from becoming SQL. Here the attacker's text becomes SQL by way of the model, upstream of any placeholder.
- For most product features, ship a fixed tool set rather than a SQL box. Text-to-SQL earns its risk only for open-ended analyst questions, over a replica, under per-user policies, with a human reading the answer before anything acts on it.

## Who this is for

You are giving an agent read access to a production database that holds more than one customer's data, and someone will eventually ask which rows it read and on whose authority. Everything below is PostgreSQL, verified against PostgreSQL 17.11; the reasoning transfers to any engine with roles and policies, but the literals do not. Skip this guide if the agent queries a database that holds only its own working data, with no per-user or per-tenant separation to preserve — you still want a timeout, but the rest is machinery you do not need.

## The problem

The standard advice is "give it a read-only user", and it fails in three distinct ways.

The first is scope. A read-only role that can `SELECT` from your `orders` table can select all of it: every customer, every note field, every amount, for every tenant. The verb is constrained and the extent is not. An agent answering "how many refunds did this account have last month" needs two rows and holds a grant on every order the company has ever taken. When a model summarizes a result set, whatever came back is in the prompt, and whatever is in the prompt is in your inference provider's request logs. There is no `GRANT SELECT ON RELEVANT ROWS`.

The second is load. Text-to-SQL produces queries nobody reviewed. A join predicate the model got slightly wrong becomes a cross join; a missing filter becomes a sequential scan over the largest table you have; a `LIKE '%...%'` on an unindexed text column becomes a full read. On a shared primary, one of those competes with the transactions your customers are waiting on. Nobody chose to run it, and no application-level retry limit prevents it, because the first attempt is the one that hurts.

The third is instruction. Rows contain text that users wrote. A support ticket body, a customer's delivery note, a product review — these arrive in the model's context as data, but a model has no mechanism that distinguishes data from instruction. OWASP calls this indirect prompt injection, a subtype of LLM01, which is the first-ranked entry in the [OWASP Top 10 for LLM Applications](https://genai.owasp.org/llmrisk/llm01-prompt-injection/). Its scenario #4 is the exact analogue of a database row: an attacker modifies a document in a repository a retrieval-augmented application reads, and when a user's query returns that content, the instructions inside it alter the model's output. When the agent's job is to write SQL, the injected instruction does not have to break out of a query. It only has to persuade the thing that writes the queries.

Those three combine badly. The layers below address them separately, because no single one of them addresses all three.

## Step by step

One path, start to finish: a constrained role, a policy keyed to the acting user, and two tools instead of a SQL prompt. Every statement here was executed against PostgreSQL 17.11 exactly as written.

One assumption runs through all of it: three roles, not one. `app_owner` owns the tables and is `NOLOGIN` — a migration identity with no connection string. `app_rw` is what the application connects as at runtime, holding DML grants but owning nothing. `agent_readonly` is the agent's own role, and is the subject of everything below. Build the example as a superuser instead and step 6 cannot be demonstrated at all, because a superuser bypasses every policy you write. `examples/connect-databases/setup_agent_role.sql` therefore runs its object-creation section under `SET ROLE app_owner`; the file's header names which statements need superuser and which do not. The other sources are `examples/connect-databases/checks.sql` (the CI assertions), `examples/connect-databases/prove_rls.sql` (the transcript), `examples/connect-databases/view_ownership.sql`, and `examples/connect-databases/query_tools.py`.

### 1. Create a login role that belongs to the agent alone

Do not reuse the application's role. The agent's grants should be readable as a list, revocable in one statement, and attributable in the server log without inference.

```sql
CREATE ROLE agent_readonly LOGIN PASSWORD 'replace-me-with-a-rotated-secret'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
```

`NOBYPASSRLS` is the default, and writing it out is still worth the line: a role with `BYPASSRLS` ignores every policy you are about to write, and so does a superuser. That is not a policy misconfiguration you can detect by reading the policies.

Then take back what every role gets for free:

```sql
REVOKE ALL ON DATABASE app FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
```

On PostgreSQL 14 and earlier, `PUBLIC` holds `CREATE` on the `public` schema, so any role that can log in can create objects there. PostgreSQL 15 [removed that default](https://www.postgresql.org/docs/15/release-15.html) for new clusters and newly created databases, but an upgraded cluster or a restored dump keeps the old grant. Revoke it regardless of version rather than checking.

### 2. Grant SELECT on named relations only

```sql
GRANT CONNECT ON DATABASE app TO agent_readonly;
GRANT USAGE ON SCHEMA public TO agent_readonly;

GRANT SELECT ON TABLE public.orders TO agent_readonly;
GRANT SELECT (id, name) ON TABLE public.accounts TO agent_readonly;
```

Column-level grants are the cheapest win in this whole guide and almost nobody uses them. `GRANT SELECT (id, name)` means a query touching `accounts.billing_email` fails with `permission denied for table accounts` rather than returning a column you forgot existed. Ten minutes with the column list of your widest table will remove more data from the agent's reach than any prompt engineering.

Notice what that grant does *not* do. `accounts` is the tenant list, and a column grant hides `billing_email` while leaving the agent free to read `id` and `name` for every customer you have. Column grants narrow rows sideways, never downwards. `accounts` needs its own policy in step 6, and so does `account_members`; a table you added only because a policy or a tool needed to join it is exactly the table that gets left uncovered.

Two things not to do. Do not `GRANT SELECT ON ALL TABLES IN SCHEMA public` — it is a one-time expansion over the tables that exist right now, so it silently understates what you meant and silently misses every table added next quarter. And do not reach for the `pg_read_all_data` predefined role: PostgreSQL's own documentation notes it "does not bypass row-level security (RLS) policies", which sounds reassuring until you notice that it hands out `SELECT` on everything, which is the part you were trying to avoid.

### 3. Attach resource ceilings to the role

```sql
ALTER ROLE agent_readonly SET statement_timeout = '5s';
ALTER ROLE agent_readonly SET lock_timeout = '2s';
ALTER ROLE agent_readonly SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE agent_readonly SET default_transaction_read_only = on;
ALTER ROLE agent_readonly SET transaction_timeout = '30s';
```

Each of these closes a different hole. `statement_timeout` aborts the runaway query. `lock_timeout` is only useful below it: PostgreSQL's docs note that with a nonzero `statement_timeout` it is "rather pointless" to set `lock_timeout` to the same or a larger value, since the statement timeout would always trigger first. `idle_in_transaction_session_timeout` kills the session an agent left mid-transaction when its own process hung, which otherwise blocks vacuum. `transaction_timeout`, added in [PostgreSQL 17](https://www.postgresql.org/docs/17/release-17.html), catches the transaction where every individual statement is fast and the transaction never ends.

Now the part most write-ups leave out. These are per-role defaults, not enforced ceilings. `statement_timeout` has a context of `user` in `pg_settings`, which means the session can change it:

```text
app=> SELECT name, context FROM pg_settings WHERE name = 'statement_timeout';
       name        | context
-------------------+---------
 statement_timeout | user

app=> SET statement_timeout = 0;
SET
```

A compromised or confused agent can lift its own timeout, and so can a generated query prefixed with `SET`. Treat `ALTER ROLE` as a good default and put the enforced limit somewhere the session cannot reach — a `query_timeout` on the connection pooler, or a proxy that terminates the connection. What the agent genuinely cannot change is its grants, which is why privileges do the load-bearing work here and timeouts are the seatbelt.

### 4. Point the agent at a read replica

A hot standby gives you an enforcement boundary that no session-level setting can undo. PostgreSQL's [hot standby documentation](https://www.postgresql.org/docs/current/hot-standby.html) states that connections to a standby "are strictly read-only; not even temporary tables may be written", that `transaction_read_only` "is always true and may not be changed", and that `SET transaction_read_only = off` produces an error. `default_transaction_read_only` on the role is a preference; a standby is a fact.

The replica also decouples the agent's worst query from your checkout path. That is the entire argument for it, and it is enough on its own.

It costs you two things. Replication lag means the agent can answer a question about an order placed four seconds ago with "no such order", so surface the lag rather than letting the model narrate a stale read as current. And long queries on a standby conflict with WAL replay: once `max_standby_streaming_delay` is exceeded, the standby cancels the conflicting query. Budget for that in the failure modes below rather than discovering it as flakiness.

### 5. Carry the acting user's identity into the session

This is the step that decides whether you built a per-user reader or a universal one. The database has to know which human the agent is acting for, and the only correct mechanism is a session variable set as a bound parameter:

```sql
BEGIN READ ONLY;
SELECT set_config('app.acting_user_id', $1, true);
```

Three details, each of which has a plausible-looking wrong version.

`set_config` rather than `SET`, because `SET` is a utility statement that cannot be prepared, so it cannot take a placeholder at all:

```text
app=# PREPARE p AS SET app.acting_user_id = $1;
ERROR:  syntax error at or near "SET"

app=# PREPARE p2 AS SELECT set_config('app.acting_user_id', $1, true);
PREPARE
```

The only way to write the first form is to build the string yourself, which means you have introduced a concatenation site into the one statement whose whole job is establishing trust.

`is_local` set to `true`, the third argument, because PostgreSQL's docs define it as applying the value "only during the current transaction". Set it to `false` and the identity persists on the connection. Under a connection pooler that is a cross-user data leak on the next borrower: PgBouncer's [feature matrix](https://www.pgbouncer.org/features.html) marks `SET`/`RESET` as never supported in transaction pooling mode, and its own summary is that transaction pooling "breaks client expectations of the server by design". A transaction-scoped setting is safe there because the pooler holds one server connection for the whole transaction.

A prefixed name with a dot — `app.acting_user_id`, not `acting_user_id`. A name with no prefix is looked up as a built-in parameter, so `SET acting_user_id = '1'` fails with `unrecognized configuration parameter "acting_user_id"`.

The identity you put here has to come from your own authenticated session, not from anything the model produced. That is the [delegated identity](/govern/delegated-identity/) problem: the agent is acting for a user, the database needs to know which one, and the answer must travel from the request that started the run rather than from a field the agent filled in. An agent that chooses its own `acting_user_id` has authentication in name only.

### 6. Enable and force row-level security

```sql
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orders FORCE ROW LEVEL SECURITY;
```

`ENABLE` alone leaves the owner exempt. PostgreSQL's [row security documentation](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) says table owners "normally bypass row security as well", and `FORCE` is what subjects the owner to its own policies. Note the limit up front: `FORCE` does not constrain a superuser or a role with `BYPASSRLS`, which "always bypass the row security system".

Be precise about what that is worth, because the next paragraph takes most of it back. **`FORCE` is not what contains the agent.** `ENABLE` plus the agent's own policy does that, and it does it whether or not `FORCE` is set, because the agent is not the owner. `FORCE` changes the behaviour of exactly one role: the one that owns the table.

**Do not ship those two statements on their own.** "Its own policies" presupposes the owner has policies, and a table with `FORCE` and no policy matching the owner default-denies against it — the same default-deny that protects you from an unidentified agent now points at your own migrations. On a table owned by an ordinary non-superuser role, which is what a real deployment looks like, `SELECT count(*) FROM public.orders` as the owner returns `0` and every write fails with `new row violates row-level security policy for table "orders"`. Reads empty, writes rejected, no configuration changed: the application is simply down. PostgreSQL's docs do not warn about this, and it does not reproduce at all if you test as a superuser, because superusers bypass RLS whatever `FORCE` says.

So give the owning role a policy in the same migration that turns `FORCE` on, before anything reads the table:

```sql
CREATE POLICY app_owner_all ON public.orders
    FOR ALL TO app_owner USING (true) WITH CHECK (true);
```

`FOR ALL` covers `SELECT`, `INSERT`, `UPDATE` and `DELETE`; `USING` governs which existing rows the owner sees and `WITH CHECK` which new rows it may write. `WITH CHECK` is optional here — omit it and PostgreSQL applies the `USING` expression to new rows as well — but write it out, because the moment you narrow `USING` to something other than `true` you want the write rule to be a decision rather than an inherited default. `examples/connect-databases/checks.sql` asserts every forced table has such a policy, and it is the first assertion in the file because this is the mistake that takes production down rather than leaking from it.

Now say the uncomfortable part out loud, because a permissive owner policy is behaviourally identical to no `FORCE` at all. Connect as `app_owner` after running the setup and you get every order, every account, and `billing_email`, with no identity set — verified. The owner can also run `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` and `DROP POLICY`, which no policy can prevent, because those are ownership rights rather than row rights.

So here is the honest accounting, and it is the sentence to take away from this step: **`FORCE` plus a permissive owner policy does not contain the owner; what it buys is that the owner's access is an explicit predicate you can read, grep and narrow instead of a built-in exemption you cannot, and the thing that actually keeps an owner-shaped connection out of your system is that the owner never logs in.**

Which is a design instruction, not a footnote. In `setup_agent_role.sql` the owner is `NOLOGIN` with no password, nothing is a member of it, and only a superuser can `SET ROLE` to it — so there is no owner DSN to copy into an agent, a cron job, or a staging config. The application connects as `app_rw`, a separate login role holding ordinary DML grants and the same `USING (true)` policy, because an application serving every tenant legitimately reads every tenant. The difference is not what the two roles can see. It is that `app_rw` does not own the tables, so its attempts to take the containment apart fail:

```text
app=> ALTER TABLE public.orders DISABLE ROW LEVEL SECURITY;
ERROR:  must be owner of table orders
app=> DROP POLICY orders_acting_user ON public.orders;
ERROR:  must be owner of relation orders
```

If your application currently connects as the role that owns its tables — which is the default in most projects that never thought about it — that is the thing to change, and it is worth more than the `FORCE` line that led you here. Assertion 4 in `checks.sql` fails the build if `app_owner` can log in.

A narrower owner predicate is the obvious alternative. Do not reach for it. `USING (true)` is right for a role whose job is migrations, backfills and incident repair: any predicate narrow enough to be worth writing is one a migration eventually violates, at which point somebody widens it under time pressure and nobody narrows it again. Keep the owner permissive, keep it offline, and spend the effort on the agent's policy, which is the one guarding data against a caller you do not control.

Then the policy:

```sql
CREATE POLICY orders_acting_user ON public.orders
    FOR SELECT
    TO agent_readonly
    USING (
        account_id IN (
            SELECT m.account_id
            FROM public.account_members AS m
            WHERE m.user_id
                  = NULLIF(current_setting('app.acting_user_id', true), '')::bigint
        )
    );
```

The `USING` expression is where the wrong forms cluster.

`current_setting('app.acting_user_id', true)` with the `missing_ok` argument, added in PostgreSQL 9.6 to allow "avoiding an error for an unrecognized parameter name, instead returning a NULL". Without it, `current_setting` raises an error when the setting was never set, so a session that forgot to establish an identity gets an exception instead of an empty result — and an exception is something an agent may retry, log, or route around. With `missing_ok`, the docs say NULL is returned instead.

`NULLIF(..., '')` around it, because NULL is not the only unset value you will meet. A setting that was never set in the session returns NULL, but one that was set and then `RESET` returns the empty string, and `''::bigint` raises `invalid input syntax for type bigint: ""`. Both were reproduced on 17.11. `NULLIF` collapses the two cases, the comparison yields NULL, the row fails the policy, and an unidentified session sees nothing. Fail closed by construction rather than by a check you have to remember to write.

Policies are permissive by default and combine with `OR`, so a second permissive policy widens access rather than narrowing it. When you want a condition every row must satisfy, write `AS RESTRICTIVE`, which combines with `AND`.

One consequence that surprises people: a policy expression runs with the privileges of the querying role, so the policy above requires `GRANT SELECT ON public.account_members TO agent_readonly`. Without it every query fails with `permission denied for table account_members`. Grant the lookup table too, and put a policy on it as well so the agent cannot enumerate the whole membership map.

Apply the same pair — `ENABLE`, `FORCE`, an `app_owner_all` policy, and an agent policy keyed to the acting user — to `accounts` and `account_members`, not just to `orders`. The setup file does all three. Half-covering the set is the most common way this ends up looking correct in review and leaking in production: the interesting table is locked down and the join table beside it hands over the shape of everything.

### 7. Expose parameterized tools instead of a SQL box

Now the interface the model actually sees. In `examples/connect-databases/query_tools.py`, each tool is a fixed SQL string with placeholders, wrapped in the transaction that establishes identity:

```python
@contextmanager
def acting_as(conn: psycopg.Connection, user_id: int) -> Iterator[psycopg.Connection]:
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise TypeError("user_id must be an int")
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            "SELECT set_config('app.acting_user_id', %s, true)",
            (str(user_id),),
        )
        yield conn


def orders_by_status(conn, user_id: int, status: str) -> list[dict[str, Any]]:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_STATUSES)}")
    with acting_as(conn, user_id) as session:
        cursor = session.execute(
            """
            SELECT id, account_id, status, total_cents, placed_at
            FROM public.orders
            WHERE status = %s
            ORDER BY placed_at DESC
            LIMIT %s
            """,
            (status, MAX_ROWS),
        )
        return cursor.fetchall()
```

The allow-list on `status` and the placeholder are doing different jobs, and conflating them is how people end up believing parameterization is a security boundary for agents. The placeholder stops the value from becoming SQL. The allow-list stops the model from asking a question the tool was never designed to answer. You need both, and neither one addresses prompt injection, which the failure modes below take up.

If you do need generated SQL, wrap it rather than editing it:

```python
cursor = session.execute(
    f"SELECT * FROM ({sql}) AS agent_query LIMIT %s",
    (MAX_ROWS,),
)
```

Appending `LIMIT 200` to model output is the intuitive move and it does not work. A generated query ending in `-- comment` swallows the clause and returns everything; one that already ends in `LIMIT 5` becomes `LIMIT 5 LIMIT 200`, a syntax error; one ending in a semicolon is a syntax error too. All three were reproduced. The subquery wrapper turns each of those into a failure instead of a leak: `SELECT id FROM orders; DROP TABLE orders` raises `syntax error at or near ";"`, an unterminated comment raises `syntax error at end of input`, and a data-modifying CTE raises `WITH clause containing a data-modifying statement must be at the top level` — the last one even for a superuser. Passing the limit as a bound parameter helps again, because psycopg then uses the extended query protocol, and its documentation states that with parameters "it is not possible to execute several statements in the same `execute()` call", while "there is no such limitation if no parameters are used".

None of that makes the SQL text trustworthy. It makes the surrounding containment survive untrustworthy text.

### 8. Prove it with two users and one code path

The test that matters is not that the query works. It is that the same code returns different rows for different people:

```text
$ python3 query_tools.py 1
-- order_summary as user 1
{'account_id': 100, 'status': 'refunded', 'order_count': 1, 'total_cents': Decimal('4900')}
{'account_id': 100, 'status': 'shipped', 'order_count': 1, 'total_cents': Decimal('12900')}
-- shipped orders as user 1
{'id': 5001, 'account_id': 100, 'status': 'shipped', 'total_cents': 12900, 'placed_at': datetime.datetime(2026, 8, 18, 22, 21, 22, 470759, tzinfo=zoneinfo.ZoneInfo(key='Etc/UTC'))}

$ python3 query_tools.py 2
-- order_summary as user 2
{'account_id': 200, 'status': 'shipped', 'order_count': 1, 'total_cents': Decimal('88000')}
-- shipped orders as user 2
{'id': 5003, 'account_id': 200, 'status': 'shipped', 'total_cents': 88000, 'placed_at': datetime.datetime(2026, 8, 18, 22, 21, 22, 470759, tzinfo=zoneinfo.ZoneInfo(key='Etc/UTC'))}
```

The `placed_at` values come from `now()` when the example data was seeded, so yours will differ; nothing else in that output does.

`examples/connect-databases/prove_rls.sql` runs the same proof in `psql`, including the case that matters most: with no identity set, `SELECT count(*) FROM public.orders` returns `0`. Run that assertion in CI — `examples/connect-databases/checks.sql` is that file, eight assertions that exit 0 together and exit 3 on the first regression. A regression here is silent — nothing errors, the agent just starts seeing more — and a test that only checks "user 1 sees their own rows" passes just as happily when user 1 sees everyone's. Run `checks.sql` as the agent's own role, never as a superuser, or every behavioural assertion in it passes vacuously.

Note what the summary above comes from. `order_summary` is a view, and views are where per-user access quietly dies:

```sql
CREATE VIEW public.order_summary WITH (security_invoker = true) AS
SELECT o.account_id, o.status, count(*) AS order_count, sum(o.total_cents) AS total_cents
FROM public.orders AS o
GROUP BY o.account_id, o.status;
```

Without `security_invoker`, a view resolves its base tables as the view's owner and returns whatever the *owner* can see. What that means for the agent depends on the owner, and it is worth knowing all three answers because two of them look like success. `examples/connect-databases/view_ownership.sql` runs them side by side on 17.11:

- Owner is a non-superuser with the permissive `app_owner_all` policy from step 6 — the configuration this guide builds. The definer view returns **every account**. This is the leak, and it is the case a reader who followed the guide is actually in.
- Owner is a non-superuser subject to `FORCE` with no policy. The definer view returns **nothing**, because the owner itself is locked out. Not safety: the same outage step 6 warns about, arriving through a view.
- Owner is a superuser. The definer view returns **every account** again, since a superuser bypasses RLS whatever `FORCE` says.

The same view created `WITH (security_invoker = true)` returns only the caller's rows in all three. `security_invoker` arrived in PostgreSQL 15; on 14 and earlier, a view over an RLS-protected table is not a safe way to expose that table to a lower-privileged role.

### 9. Log the query and the user who caused it

When something goes wrong you will want to answer one question: which request, from which person, produced which SQL. Assemble it from three parts.

Tag the session so the log carries the caller without parsing SQL:

```python
conn.execute(
    "SELECT set_config('application_name', %s, false)",
    (f"agent:order-triage:user={user_id}",),
)
```

`application_name` is the `%a` escape in `log_line_prefix`, so every logged statement from that session is attributable. Then turn on statement logging for this role only, rather than for the whole cluster:

```sql
ALTER ROLE agent_readonly SET log_statement = 'all';
ALTER ROLE agent_readonly SET log_min_duration_statement = '250ms';
```

`log_statement = 'all'` logs every statement, and for clients using the extended query protocol the values of the bind parameters are included as well. Only superusers and roles holding the relevant `SET` privilege can change these, so the agent cannot quietly turn its own audit trail off. Do not reach for `pg_stat_statements` here: it normalizes literals into `$1` symbols by design, which is exactly right for finding slow query shapes and exactly wrong for finding out which account id an agent read.

The third part is not in the database. Correlate on a request id that your agent generates before the first query and writes into both its own logs and `application_name`. The [govern pillar](/govern/) treats that correlation as a first-class design problem rather than a logging afterthought, and it is the difference between a two-hour incident and a two-week one.

## Decision table

| Option | When it wins | Blast radius | Expressiveness | Operational cost |
| --- | --- | --- | --- | --- |
| Fixed parameterized tools | The questions are countable — status lookups, per-account summaries, the twenty things support actually asks. Default choice. | Bounded by construction: the agent cannot express a query you did not write. | Low. A new question needs a code change and a deploy. | Low at runtime, ongoing at design time. You will maintain a tool per question shape. |
| Materialized read model | High query volume, or the source is a normalized schema no model reasons about well, or the primary must not be touched at all. | Bounded by what you copied. Rows never materialized cannot leak. | Medium. Anything in the model is fast; anything outside it does not exist. | Highest up front: a pipeline, a freshness contract, and a second place where per-user filtering has to be right. |
| Free-form text-to-SQL | Genuinely open-ended analyst questions over a wide schema, on a replica, under per-user policies, with a human reading the answer. | Whatever the role can reach. Contained by grants and RLS, not by the query. | Highest. That is the entire reason to consider it. | Deceptively low to build, high to operate: injection review, query review, cost control, and an accuracy problem that never fully closes. |

Here is the judgement the table implies, stated plainly: for anything that ships inside a product, use tools, not text-to-SQL. Three mechanics from the sections above force that conclusion. First, generated SQL cannot be validated for correctness by the thing that generated it, and a query that is wrong but plausible returns a confident number rather than an error — the worst possible failure for a system whose output people trust. Second, expressiveness is exactly the attack surface: the property that lets a model answer a question you did not anticipate is the property that lets an injected instruction ask one. Third, every containment layer that makes text-to-SQL survivable — replica, RLS, grants, timeouts, row caps — is a layer you want under the tool set anyway, so the tools are not paying for their safety while text-to-SQL is.

Text-to-SQL earns its risk in one shape: an internal analyst tool, over an analytics replica, where the user is authenticated and the policies already scope the data to them, where a wrong answer is caught by a human who knows the domain, and where the alternative is genuinely "the analyst files a ticket and waits three days". That is a real and valuable case. It is not the case you are in when you are adding a chat box to a customer-facing dashboard.

## Checklist

- [ ] The agent has its own login role, and `SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = 'agent_readonly'` returns false for all four.
- [ ] `REVOKE ALL ON SCHEMA public FROM PUBLIC` has run in this database, on every version.
- [ ] Every grant to the role names a relation. A grep for `ALL TABLES IN SCHEMA` and `pg_read_all_data` in your migrations returns nothing.
- [ ] Column-level grants exclude the columns the agent has no business reading, and a query touching one fails with a privilege error.
- [ ] `ALTER ROLE ... SET statement_timeout` is set, and a second enforced timeout exists outside the session at the pooler or proxy.
- [ ] Agent traffic resolves to a read replica, verified by `SELECT pg_is_in_recovery()` returning true from the agent's own connection.
- [ ] Every table holding per-user or per-tenant data has both `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`.
- [ ] Every table with `FORCE` also has a policy covering its owning role, verified before deploy — `FORCE` with no owner policy default-denies against your own application, emptying its reads and rejecting its writes.
- [ ] The role that owns the tables is `NOLOGIN`, has no password, and has no members. It holds a permissive policy and can disable RLS outright, so there must be no connection string that reaches it.
- [ ] The application connects as a role that does not own its tables, so `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` and `DROP POLICY` fail for it with `must be owner of table`.
- [ ] Lookup and tenant-listing tables are covered too, not just the table holding the interesting rows. A column grant hides a column; it does not stop the agent enumerating every tenant.
- [ ] Every policy reads the identity as `NULLIF(current_setting('app.acting_user_id', true), '')::bigint` or the equivalent for its key type.
- [ ] The identity is set with `set_config(..., true)` inside the transaction that uses it, never with a concatenated `SET` and never session-scoped on a pooled connection.
- [ ] Every view the agent can read was created `WITH (security_invoker = true)`, or the guide's reason for the exception is written down.
- [ ] A CI test asserts that a session with no identity set returns zero rows, and that user A's tool call never returns user B's rows.
- [ ] The identity comes from the authenticated request, and there is no code path where the model supplies it.
- [ ] Every result set is capped by a bound `LIMIT` on a wrapping query, not by string-appending a clause to generated SQL.
- [ ] Row text that reaches a prompt is marked as untrusted data, and no tool call is issued on the strength of instructions found in a row.
- [ ] `application_name` identifies the agent, the task, and the acting user, and `log_statement` is set on the role rather than the cluster.
- [ ] Rotating the agent's database password is a runbook step someone has actually executed, not a plan.

## Failure modes

### The agent returns another tenant's rows and nothing errors

**Symptom:** no exception, no privilege error, no anomaly in latency. A user reports seeing a row they should not have, or nobody reports anything and you find it in a log months later.

**Cause:** either the querying role is not subject to the policies, or the rows are being fetched on behalf of a role that sees more than it does.

The first has four forms. The connection is the table owner's and the table has `ENABLE` but not `FORCE ROW LEVEL SECURITY`. The connection is the table owner's, `FORCE` *is* set, and the owner holds the permissive `app_owner_all` policy from step 6 — the configuration this guide ships, and the reason step 6 insists the owner be `NOLOGIN`. The role holds `BYPASSRLS`, often granted by someone debugging a policy that "wasn't working". Or the role is a superuser. Note that the second form also arrives through role membership: `GRANT app_owner TO someone` hands that someone the owner's policy on every forced table.

The second is a view without `security_invoker`, and it is worth separating out because it bites even when the first three are all clean. Such a view returns the *owner's* row set, and under the configuration step 6 prescribes the owner holds a permissive `app_owner_all` policy — so the owner sees every row, and so does anyone you grant the view to. The owner does not have to bypass RLS for this to leak; it only has to be allowed to see more than the caller, which is the normal state of affairs for an application role.

**Fix:** audit each one explicitly — `relrowsecurity` and `relforcerowsecurity` in `pg_class` for the tables, `reloptions` for the views, `rolbypassrls` and `rolsuper` in `pg_roles` for the roles. Assertions 5 and 2 in `examples/connect-databases/checks.sql` do the first two as a CI gate, and `examples/connect-databases/view_ownership.sql` reproduces the view case under all three ownership models so you can see which one you are in. Then add the zero-rows-without-identity assertion, because this class of bug is invisible to every test that checks a positive case.

### The application reads zero rows the moment you turn on RLS

**Symptom:** immediately after a migration that adds row-level security, the application's own queries return empty result sets and every write fails with `new row violates row-level security policy for table "orders"`. Nothing was denied at the privilege level, the role still owns the table, and the same migration passed on a laptop.

**Cause:** the owner-policy trap from step 6 — `FORCE` applied with policies written only `TO agent_readonly`. It passed on the laptop because the laptop's tables are owned by `postgres`, and a superuser bypasses RLS whatever `FORCE` says, so the entire failure is invisible in any environment where the schema was created by a superuser.

**Fix:** create the owner's policy in the same migration as `FORCE`, never in a follow-up: `CREATE POLICY app_owner_all ON public.orders FOR ALL TO app_owner USING (true) WITH CHECK (true);`. Then stop the class of bug rather than the instance — assertion 1 in `examples/connect-databases/checks.sql` fails the build for any forced table with no policy covering its owner, and run your migration tests against a non-superuser owner so this reproduces before deploy instead of after.

### A generated query takes the database down

**Symptom:** connection pool exhaustion and rising latency across the whole application, starting from a single agent request. The slow query log shows one statement, running for minutes, with a plan nobody would have written.

**Cause:** the model produced a cross join, an unfiltered scan of the largest table, or an aggregate over a partition set it did not know was a partition set. RLS does not help — a policy narrows which rows are visible, not how much work finding them costs. In the example schema a `CROSS JOIN` against a policy-protected table still runs; it just returns the caller's rows.

A row cap does not help either, and it is worth seeing why, because `LIMIT` looks like a work cap. A plan can stop early only when nothing above the `Limit` node needs the whole input. Run the two `EXPLAIN`s in `prove_rls.sql` from the agent's own connection: `SELECT o.id FROM orders o CROSS JOIN orders o2 LIMIT 200` comes back with a `Limit` node at an estimated total cost of 18.94, because it stops as soon as it has 200 rows. Add `ORDER BY o.total_cents` and the same query estimates 5,939.28, because the `Sort` underneath the limit has to consume its whole input — 105,625 rows on the planner's estimate — before it can hand over the first one. `GROUP BY`, `DISTINCT` and `count(*)` do the same thing. Models write `ORDER BY`.

**Fix:** the replica plus `statement_timeout` plus the pooler-level timeout, in that order of importance. Add `EXPLAIN` before execution if you run generated SQL at all, and refuse plans whose estimated cost exceeds a threshold — an estimate is not a guarantee, but a plan estimating a billion rows is a reliable enough signal to reject on.

### An injected instruction in a customer's own data rewrites the query

**Symptom:** the agent runs a query no user asked for. The SQL in your log is valid, correctly parameterized, and within the agent's grants, which is why nothing flagged it. It just answers a different question — often a broader one, sometimes one whose result the agent then puts in a reply.

**Cause:** a row contained text addressed to the model. `note`, `subject`, `description`, a scraped page, a PDF the agent read a step earlier. The model's next output is a query, and the instruction changed what that query says.

**Fix:** start with the fix that is not available. Parameterization does not stop this, and the reason is worth being precise about. A placeholder solves one specific problem: it keeps a *value* from being reinterpreted as *syntax* by the parser. `WHERE status = $1` with `$1 = "'; DROP TABLE orders --"` is safe because the driver sends the value out of band and the server never parses it as SQL. In a text-to-SQL agent, the untrusted text never occupies a value position. It goes into the prompt, and the model emits a new query — one that is well-formed, that binds its own parameters correctly, and that your driver has no reason to reject. The injection happened one layer above the driver, so the driver's defense never engages. Placeholders defend the boundary between data and code inside a query you wrote. Here the attacker's influence is on which query gets written, and no amount of quoting reaches that.

What does work is the containment this guide is about, and OWASP's own mitigations point the same way: enforce privilege control and least privilege access, and require human approval for high-risk actions. Concretely: the role cannot read what it must not read, so a rewritten query returns the same rows or a privilege error. RLS is keyed to the acting user, so a broadened query is still scoped to one person. Tools with allow-listed arguments give the model no place to put an arbitrary query. Untrusted row text is fenced and labelled as data in the prompt, and — the rule that catches the rest — a tool call is never issued because a row said to issue it. The [failure pillar](/fail/) covers what happens when that last rule is missing and the agent starts acting on content it retrieved.

### One user's identity leaks into the next user's session

**Symptom:** intermittent and load-dependent. A user occasionally sees another user's data, most often under concurrency, never reproducibly, and never in development where the pool holds one connection.

**Cause:** the identity was set with `set_config(..., false)` or a bare `SET`, both of which are session-scoped. Under a pooler in transaction mode, the server connection returns to the pool with the setting still on it, and the next borrower inherits it. PgBouncer documents `SET`/`RESET` as not supported in transaction pooling precisely because session state does not survive the way applications assume.

**Fix:** `set_config(..., true)` inside an explicit transaction, always. Add a check that refuses to run a tool outside a transaction. If you must use session-level settings, use session pooling and pay for the connections, and be explicit about which mode your pooler is in — this bug is a two-line configuration difference away in either direction.

### The policy raises an error instead of filtering

**Symptom:** queries fail with `unrecognized configuration parameter "app.acting_user_id"` or `invalid input syntax for type bigint: ""`, often only on the first query of a connection or only after some code path calls `RESET`.

**Cause:** `current_setting` was called without the `missing_ok` argument, so an unset parameter raises rather than returning NULL. Or `missing_ok` is there but the value is the empty string a `RESET` leaves behind, and the cast to `bigint` fails on it.

**Fix:** `NULLIF(current_setting('app.acting_user_id', true), '')::bigint`, which handles both. This is more than tidiness. An error is a signal your agent may retry, catch, or route around — some frameworks will helpfully fall back to a different code path — whereas an empty result set is a correct answer to a question asked by nobody. Make the unidentified case return nothing, not raise.

### The replica cancels the agent's query in the middle of an answer

**Symptom:** `ERROR: canceling statement due to conflict with recovery` from the agent's connection, clustered around periods of heavy write traffic on the primary, and absent entirely in a test environment with an idle primary.

**Cause:** a query on a hot standby held a snapshot that blocked WAL replay for longer than `max_standby_streaming_delay`, so the standby cancelled it to keep replaying. This is the standby doing its job, not a fault.

**Fix:** keep agent queries short. The default `max_standby_streaming_delay` is 30 seconds, so a five-second `statement_timeout` usually fires first — but not always, because the parameter caps the total time allowed to apply received WAL, not the lifetime of any one query, so a query that arrives after another has already burned the budget gets much less grace. Where a longer analytical query is legitimate, raise `max_standby_streaming_delay` deliberately (it is a `postgresql.conf` setting, not something a session can change), or turn on `hot_standby_feedback`, which the docs say "can be used to eliminate query cancels caused by cleanup records, but can cause database bloat on the primary for some workloads". Retry the cancellation once; do not retry it in a loop, which turns a transient conflict into sustained load on a standby that is already behind.

### Nobody can say which question produced which query

**Symptom:** an incident review where the SQL is in the server log, the user's request is in the application log, and no field joins them. Somebody starts matching on timestamps.

**Cause:** logging was turned on per system rather than designed as one trail. The database knows the role and the statement, the application knows the user and the prompt, and neither knows the other's identifier.

**Fix:** generate a request id at the start of the run, put it in `application_name` alongside the acting user, and log it with the prompt on the application side. Set `log_statement` on the agent's role so the volume stays proportionate. And keep the model's input and output for the run, not just the SQL: when a query is legitimate but wrong, the SQL text tells you nothing about why it was written.

## Doing this at scale

Everything above is one database and one agent. The cost that actually accumulates is the multiplication. Ten agents against six databases is sixty role-and-grant pairs, sixty passwords with a rotation schedule, sixty sets of policies whose drift nobody notices until a test that never existed would have caught it. The identity plumbing gets worse in a different way: `set_config` works because the agent process knows which user it is acting for, and that fact has to travel from an authenticated request through however many hops the agent takes, without any hop being able to change it. The moment a second service can set `app.acting_user_id`, per-user access is a convention rather than a control.

Then there is the credential itself. A DSN with a password in it is a bearer token with no scope, no expiry, and no per-action audit — the database equivalent of the app password problem covered in the [Gmail guide](/connect/gmail/). It appears in an environment variable, a Kubernetes secret, a CI log, and a developer's shell history, and rotating it means a coordinated restart of everything that holds it. `pg_stat_activity` will tell you `agent_readonly` ran a query; it will not tell you which of your four agents did, or for whom.

[Agentic Fabriq](https://agenticfabriq.com) is a control layer for exactly that middle. The agent holds a token for the layer rather than a database password; the layer holds the credential, evaluates policy per request, and records the call as an action by a named agent on behalf of a named user. The agent calls a connection by name:

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        summary = await af.invoke_connection(
            "orders_db",
            method="order_summary",
            parameters={"acting_user_id": 1},
        )
        print(summary)


asyncio.run(main())
```

The runnable version is `examples/connect-databases/fabriq_query.py`. Connection and method names are per-deployment, so run `afctl tools list` against your own gateway instead of copying `orders_db` and `order_summary` on faith.

Be clear about the division of labour, because a control layer is not a substitute for the database work. The grants, the policies, the `FORCE ROW LEVEL SECURITY`, the timeouts, the replica — those are still yours, and they are still the only thing standing between a rewritten query and your data. What a layer like [Agentic Fabriq](https://agenticfabriq.com) removes is the part that scales badly: the credential in the agent process, the identity that has to be propagated by hand, and the audit trail assembled after the fact from three log streams. If you would rather own that lifecycle yourself, everything in the previous sections still stands unchanged.

## Further reading

The [connect pillar](/connect/) covers how this pattern generalizes to other systems: the taxonomy differs — OAuth scopes for a SaaS API, roles and policies for a database — but the ordering does not, and neither does the rule that the acting user's identity has to reach the system doing the enforcing. The [govern pillar](/govern/) treats least privilege and delegated identity as policy problems rather than per-integration decisions, and the [fail pillar](/fail/) collects what happens when a containment layer is missing.

Primary sources for everything asserted above:

- [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) — superuser and `BYPASSRLS` bypass, owner bypass and `FORCE`, permissive versus restrictive combination, and the default-deny behaviour when no policy exists.
- [System Administration Functions](https://www.postgresql.org/docs/current/functions-admin.html) — the `current_setting(setting_name [, missing_ok])` and `set_config(setting_name, new_value, is_local)` signatures and semantics.
- [Client Connection Defaults](https://www.postgresql.org/docs/current/runtime-config-client.html) — `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `transaction_timeout`, and `default_transaction_read_only`.
- [PostgreSQL 17 release notes](https://www.postgresql.org/docs/17/release-17.html) — the addition of `transaction_timeout`.
- [PostgreSQL 15 release notes](https://www.postgresql.org/docs/15/release-15.html) — removal of `PUBLIC` creation permission on the `public` schema, and views controlled by the caller's privileges.
- [CREATE VIEW](https://www.postgresql.org/docs/current/sql-createview.html) — `security_invoker` and `security_barrier`, and how RLS policies resolve through a view.
- [Predefined Roles](https://www.postgresql.org/docs/current/predefined-roles.html) — what `pg_read_all_data` grants and that it does not bypass RLS.
- [Hot Standby](https://www.postgresql.org/docs/current/hot-standby.html) — standby connections being strictly read-only, and query cancellation under `max_standby_streaming_delay`.
- [Error Reporting and Logging](https://www.postgresql.org/docs/current/runtime-config-logging.html) — `log_statement`, `log_min_duration_statement`, and the `%a` application-name escape.
- [pg_stat_statements](https://www.postgresql.org/docs/current/pgstatstatements.html) — constant normalization into `$1` symbols.
- [psycopg 3: differences from psycopg2](https://www.psycopg.org/psycopg3/docs/basic/from_pg2.html) — multiple statements per `execute()` under the extended versus simple query protocol.
- [PgBouncer feature matrix](https://www.pgbouncer.org/features.html) — `SET`/`RESET` support by pooling mode.
- [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) — direct and indirect injection, and the privilege-control and human-approval mitigations.

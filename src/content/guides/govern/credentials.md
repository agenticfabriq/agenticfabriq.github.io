---
title: Credential Vaulting and Rotation for Agents
description: Why an environment variable cannot express per-user access, how to issue short-lived credentials per request, and how to rotate and revoke without downtime.
pillar: govern
updated: "2026-08-18"
keywords:
  - agent credential management
  - secret rotation agent
  - short lived credentials
  - workload identity federation
  - credential vaulting
  - zero downtime secret rotation
relatedGuides:
  - govern/oauth-flows
  - govern/audit
  - fail/token-leakage
howTo:
  - name: Count what the environment variable actually reaches
    text: Write down every resource the process-wide credential can touch and every user whose data those resources hold, because that product is the cost of one leak.
  - name: Give the agent a workload identity it cannot choose
    text: Replace the stored secret with a projected Kubernetes token, an IMDSv2 credential, or a CI OIDC token, and pin the trust chain on both the audience and the subject.
  - name: Issue a credential per request, scoped to one user and one resource
    text: Have the agent present its own identity to a broker that checks the calling user's entitlement and returns a credential valid for one resource and a few minutes.
  - name: Redact at the handler's formatter, not at the call site
    text: Register every credential value the issuer mints and scrub the fully rendered log line, so library loggers and tracebacks are covered by the same code path.
  - name: Rotate with an overlap window you can compute
    text: Create the new credential, add it at the resource, test it, promote it for signing, drain for the longest outstanding lifetime, then retire the old one.
  - name: Revoke a single credential without rotating anything
    text: Give the verifier a way to reject one credential by id or to drop the underlying principal, and test that path before you need it.
faq:
  - question: Why are environment variables bad for storing agent credentials?
    answer: A process has one environment, so the same string is returned for every user, and its authority must therefore be the union of what every user may reach. The variable also survives into crash dumps, /proc/PID/environ, every subprocess, and container inspection output.
  - question: What is the difference between rotating a credential and revoking it?
    answer: Rotation replaces a credential on a schedule and normally leaves the old one valid through an overlap window. Revocation makes one specific credential stop working immediately. A system that can only rotate cannot end an incident, because the leaked credential stays valid until the overlap window closes.
  - question: How do you rotate a secret without downtime?
    answer: Add the new credential at the resource before anything signs or authenticates with it, test it, promote it for new issuance, wait out the longest lifetime any outstanding old credential can have, and only then retire the old one. Retiring before that wait is what causes the outage.
  - question: How long should an agent's credentials live?
    answer: Short enough that expiry alone bounds a leak you never detect. A credential scoped to one user and one resource for five minutes is reachable for five minutes whether or not anyone notices it leaked, which is the only property that does not depend on your detection speed.
  - question: Can an AI agent leak a credential through its own output?
    answer: Yes. Anything in the model's context is an input the model may reproduce in a tool argument, a summary, or an echoed error, and it is also copied into the provider's request logs and into every later turn of the same conversation. Keep credentials below the layer the model can read.
---

## TL;DR

- A process has one environment. `os.environ["DATABASE_URL"]` returns the same string for every request and every user, so its authority has to be the union of what every user may reach. Per-user entitlement is a per-request variable; an environment variable is a per-process constant. They cannot be the same object.
- Pulling a long-lived secret out of a vault at startup gets it out of your repository and gets reads audited. It does not change what the credential reaches or how long it lives: same reach, same lifetime, same union. That is a vault used as a filing cabinet.
- The pattern that works: the agent authenticates as *itself* with an identity it did not choose — a Kubernetes projected service account token, an IMDSv2 credential, a CI OIDC token — and trades it for a credential scoped to one user, one resource, and a few minutes. It never persists the result.
- Zero-downtime rotation is a six-step ordering, and the overlap window has a computable length: the longest lifetime any outstanding credential can have. Retiring the old credential before that window closes is the outage.
- Rotation and revocation are different levers. Rotation on its own leaves the leaked credential working until the overlap closes, which is precisely the incident it was supposed to answer.

## Who this is for

You are running an agent that acts for more than one person against a system that holds more than one person's data, and today it authenticates with a secret that was set once and has not changed since. This guide covers what that secret costs, how to replace it with per-request issuance, and how to rotate and revoke afterwards. Skip it if your agent is single-tenant, unattended, and touches exactly one resource whose entire contents every operator may already read — there the environment variable is honest about what it grants, and the machinery below buys you a shorter blast radius you did not have.

## The problem

Start with the mechanism, because the usual framing ("don't hardcode secrets") hides it. A process has exactly one environment block. `os.environ["DATABASE_URL"]` is a constant with respect to the request: same value on every thread, for every user, for the life of the process. Entitlement is not constant with respect to the request — Amara may read orders, Bo may read support tickets. A credential that lacked Bo's access would fail Bo's request, so nobody ships one. The only value that satisfies every request is the supremum of all of them, and the agent runs every request with it.

The consequence is not that the secret might leak. It is that authorization has moved out of the credential and into your code. The database no longer enforces that Amara cannot read Bo's rows; a `WHERE customer_id = :customer` clause in your query does. That clause is now the boundary, in every query, forever, and in an agent a large share of those queries are model-generated. `examples/govern-credentials/issuing_broker.py` makes this concrete — `union_of_entitlements()` is that environment variable written out as a function.

### Where an environment secret goes without being logged

The union problem is what makes environment secrets *wrong*. The following is what makes them *leaky*, and none of these require anyone to write `print(os.environ)`.

- **Process listings.** On Linux, `/proc/PID/environ` holds the environment, and access is governed by a `PTRACE_MODE_READ_FSCREDS` check, which the process's own user passes. Worse, [proc_pid_environ(5)](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html) states it contains "the initial environment that was set when the currently executing program was started via execve(2)", and that if the process later modifies its environment "this file will not reflect those changes". Reading the secret and then deleting it from `os.environ` does not remove it from `/proc/PID/environ`.
- **Subprocesses.** `subprocess.run(["git", "clone", url])` with no `env=` argument passes the entire parent environment to the child. Every helper binary, every shell-out, every hook gets the full secret set. An agent that runs model-chosen shell commands is handing the credential to model-chosen code.
- **Container inspection.** Anyone who can talk to the Docker daemon can read the variables you passed at run time. It takes one command, and you can confirm it in ten seconds: run a container with `-e DB_PASSWORD=...`, then `docker inspect NAME --format '{{json .Config.Env}}'` and read the value back out. See [docker inspect](https://docs.docker.com/reference/cli/docker/inspect/) for the command's other formats.
- **Crash dumps.** The environment block lives in process memory, so anything that captures process memory captures it. Check what your crash reporter attaches before you assume it does not.
- **Startup logs.** Config-dump banners, `set -x` in an entrypoint script, and a bare `env` in a CI step all print the whole block. If your CI masks secrets in log output, it masks the values it was registered with — which a credential minted at run time never was.

### Blast radius, as arithmetic

Take an agent serving 400 users against one PostgreSQL database with nine schemas, one per business area. Some user needs each schema, so the role behind `DATABASE_URL` can read all nine. The exposure of one leaked credential is the product of three terms:

```text
exposure = resources reachable  ×  subjects included  ×  seconds valid
```

- Environment variable: 9 × 400 × unbounded.
- Long-lived secret fetched from a vault at startup: 9 × 400 × unbounded. The vault moved where the string is kept. It did not change a single term.
- Credential issued per request, scoped to one schema and one user, valid 300 seconds: 1 × 1 × 300.

Those numbers are a worked example, not a measurement — substitute your own. What generalizes is which narrowing buys what. Scoping to one resource divides the first term by nine. Scoping to one subject divides the second by four hundred. Those two only pay off if you find out about the leak, because a credential you never revoke keeps whatever reach it has. Capping the lifetime is different in kind: it converts an unbounded term into a bounded one, and it does that whether or not anyone notices. That is the argument for short lifetimes over careful scoping, if you are only going to do one. Do both.

### The vault that is a filing cabinet

A vault earns its keep by *issuing* credentials, not by *storing* them. HashiCorp Vault's database secrets engine, for instance, generates database credentials dynamically per role and attaches a lease to them; `vault lease revoke LEASE_ID` revokes them at the database, and the role's `default_ttl` and `max_ttl` fields bound how long they can live ([database secrets engine](https://developer.hashicorp.com/vault/docs/secrets/databases)). That is a different object from a `kv` path holding one password that ten services read at boot. The second shape is worth something — the secret is no longer in a repository, and reads are audited — but the credential in the process is still long-lived, still the union, and still reaches everything. If your migration plan ends at "read it from the vault instead of the environment", you have bought the audit log and none of the blast radius.

## Step by step

The path below replaces a long-lived environment secret with per-request issuance in a small agent, then rotates the signing key with zero downtime. It runs locally with no cloud account: `examples/govern-credentials/issuing_broker.py` and `examples/govern-credentials/rotate_with_overlap.py`. The crypto in those files is a stand-in — swap `Broker` for AWS STS, GCP STS, or Vault and the shape does not change.

### 1. Count what the environment variable actually reaches

Before changing anything, write down two lists: every resource the current credential can touch, and every user whose data those resources hold. Multiply. That number is what one leak costs today, and it is the only baseline against which the rest of this work can be judged. Do it from the grant, not from the code — what the agent *currently calls* is a subset of what it *may* call, and an attacker gets the second list.

Then check the two places the number is usually worse than expected. On EC2, [you can only attach one IAM role to an instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html), and every process, container, and sidecar on that instance shares it. In AWS IAM Roles Anywhere, the [account is the trust boundary](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html): "Certificates issued by any trust anchor in the account can be used to assume any target role in that same account, unless you specify conditions in the role's trust policy."

### 2. Give the agent a workload identity it cannot choose

The agent stops holding a resource secret and starts proving who it is. Every mechanism below issues a short-lived credential; what distinguishes them is the trust chain, and the trust chain is what you have to reason about, because it is what an attacker attacks.

**Kubernetes projected service account tokens.** The kubelet requests a token from the API server's TokenRequest API and mounts it into the pod. In the volume spec, `expirationSeconds` "defaults to 1 hour and must be at least 10 minutes (600 seconds)", and `audience` "defaults to the identifier of the API server" ([projected volumes](https://kubernetes.io/docs/concepts/storage/projected-volumes/)); an administrator can cap the maximum with `--service-account-max-token-expiration` on the API server. The token "expires either when the pod is deleted or after a defined lifespan (by default, that is 1 hour)", and the kubelet refreshes the file before expiry ([service account admin](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)). *Trust chain:* the kubelet asserts pod identity, the API server signs it, and your verifier must check `aud` — a token minted for audience A must be rejected by service B, which is the entire reason the field exists.

**Instance metadata identity.** On EC2 the workload gets a session token with `PUT http://169.254.169.254/latest/api/token` carrying `X-aws-ec2-metadata-token-ttl-seconds` (maximum six hours, 21,600 seconds), then reads `iam/security-credentials/ROLE-NAME` with `X-aws-ec2-metadata-token` on the GET ([IMDS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-v2-how-it-works.html), [retrieve security credentials](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-security-credentials.html)). *Trust chain:* physical placement. The hypervisor answers that address differently per instance, so anything that can make an HTTP request from inside the instance is the instance. That is why an SSRF bug is credential theft, and why AWS warns directly on that page to "ensure that you don't expose your credentials when the services make HTTP calls on your behalf", naming HTTP proxies and XML processors. IMDSv2's two defences are mechanical: `PUT` requests are rejected if they carry an `X-Forwarded-For` header, and the response to a `PUT` has a response hop limit of `1` at the IP level by default, so the session token cannot cross an extra network hop — which is also why containerised workloads sometimes have to raise it. Note that the granularity is the instance, not the process: every container on the host is the same principal.

**OIDC-federated tokens in CI.** In GitHub Actions, a job that grants `id-token: write` in its `permissions` block gets `ACTIONS_ID_TOKEN_REQUEST_URL` and `ACTIONS_ID_TOKEN_REQUEST_TOKEN` in its environment, and can fetch a JWT issued by `https://token.actions.githubusercontent.com` whose `sub` encodes the repository and ref, in forms like `repo:ORG-NAME/REPO-NAME:ref:refs/heads/BRANCH-NAME` and `repo:ORG-NAME/REPO-NAME:environment:ENVIRONMENT-NAME` ([OIDC reference](https://docs.github.com/en/actions/reference/security/oidc)). You then exchange it: AWS `AssumeRoleWithWebIdentity`, whose `DurationSeconds` ranges from 900 to 43,200 and [defaults to 3600](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html), bounded by the role's maximum session duration; or Google's STS at `https://sts.googleapis.com/v1/token` with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange` and an audience of the form `//iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID` ([workload identity federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds)). *Trust chain:* your cloud trusts GitHub's issuer, and your role's trust policy narrows which token it accepts. This is where the mistake lives, so it gets its own failure mode below.

**X.509 for workloads outside a cloud.** AWS IAM Roles Anywhere registers your CA as a trust anchor, and a workload presenting a certificate from it calls `POST /sessions` for temporary credentials; `durationSeconds` ranges from 900 to 43,200, and the effective value is `min(profileDurationSeconds, createSessionDurationSeconds)` ([CreateSession](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/authentication-create-session.html)). *Trust chain:* your private CA, which means the CA's issuance policy is now an IAM policy, and everything the CA will sign a certificate for is a principal in your account.

For an agent that acts for a *named human* rather than for itself, the identity above is only half the story — it authenticates the workload, not the user. Delegated grants are the other half, and the [govern pillar](/govern/) is where OAuth flows and least-privilege scope design belong; the worked example is [connecting an agent to Gmail](/connect/gmail/).

### 3. Issue the credential per request, scoped to one user and one resource

Now the broker. It verifies the agent's workload identity, looks up the *calling user's* entitlement, and mints a credential bound to one subject, one resource, and a short expiry:

```python
def issue(self, identity, subject, resource, ttl_seconds=300, kid=None):
    if identity not in self.trusted_identities:
        raise BrokerError("unrecognised workload identity")
    allowed = self.entitlements.get(subject, set())
    if resource not in allowed:
        # The check an environment variable cannot perform, because an
        # environment variable does not know which user the request is for.
        raise BrokerError(f"{subject} is not entitled to {resource}")
    ...
```

The agent side is the part that is easy to get wrong, and the rule is one sentence: the credential is a local variable. Not a field on a long-lived client object, not a module-level cache, not a file, not a row.

```python
def handle_request(broker, resource, subject):
    identity = read_workload_identity()
    credential = broker.issue(identity, subject=subject, resource=resource.name, ttl_seconds=300)
    claims = resource.authorize(credential.token, subject=subject)
    return f"read {resource.name} as {claims['sub']} (jti {claims['jti']})"
```

Running `issuing_broker.py` shows what changed. Amara's credential is refused by the support database and refused when presented for Bo, and the broker refuses outright to mint a support credential for Amara:

```text
== the same agent, the wrong user ==
  broker refused to issue: amara@example.com is not entitled to db:support

== a credential that leaks reaches one user and one resource ==
  db:support rejected it: credential is for 'db:orders', not 'db:support'
  db:orders rejected it for another subject: credential is for 'amara@example.com', not 'bo@example.com'
```

Two design notes that matter more than the code. First, `read_workload_identity()` reads the token from disk on every call rather than caching it at import, because the kubelet rewrites that file. A process that reads it once at startup works for an hour and then fails, which is the same bug as caching an access token past its expiry. Second, the broker records every value it mints in `self.minted`. That set is not bookkeeping; it is what step 4 feeds to the log redactor. The two halves live in separate example files so each is readable on its own — in a deployment they are one object, because a redactor that is not fed by the issuer redacts the credentials you remembered to tell it about.

### 4. Keep the credential out of the logs and out of the model

Call-site redaction fails for a reason that has nothing to do with discipline: you do not own all the call sites. Your HTTP library logs request headers at DEBUG. Your framework logs the connection string when a connection fails. A `logging.exception` renders a traceback in which the credential appears as an argument to the frame that raised. Redacting at every `log.info` you wrote leaves all of those.

Redacting at the *logger* does not fix it either, and this is worth internalising because it looks like it should. A filter added to a logger is consulted only for records logged directly on that logger; a record created on a descendant logger propagates to the ancestor's handlers without ever passing the ancestor's filters ([logging flow](https://docs.python.org/3/howto/logging.html#logging-flow)). `examples/govern-credentials/redacting_logger.py` prints the difference:

```text
-- B. logging.Filter on the root logger: LEAKED
     httplib.transport DEBUG GET /v1/orders authorization=Bearer v1.k1.eyJzdWIiOiJhbWFyYSJ9.G3nR4pQ7wZk1s0Tq
```

The point of interception is the handler, and the most complete form is a `Formatter`, because it runs after the message, its `%`-style arguments, and any traceback have all been rendered into one string:

```python
class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return self._registry.scrub(super().format(record))
```

A filter on the handler is better than a filter on a logger, but it still has two structural blind spots that the same example demonstrates: it cannot reach exception text, which the formatter renders later, and it can only scrub arguments that are already strings, so `log.warning("failed: %s", exc)` slips an exception object past it to be stringified inside `getMessage()`. Coercing every argument to a string inside the filter would change what `%d` and `%r` produce. Use the formatter.

Scrub by *value*, not by pattern. The issuer knows the exact strings it minted, so it can do exact replacement; pattern matching only catches shapes you thought of. The honest limit is that a value-based redactor knows only what it was told, which is why issuance and redaction share one registry — and why the final demo in that file, where a value is never registered, leaks:

```text
-- D. the same formatter, value never registered: LEAKED
```

**Then there is the hazard that is specific to agents.** A credential that reaches the model's context is disclosed to whatever the model writes next. This is not a jailbreak; it is the model doing its job on its input. The credential can come back out in a tool-call argument, in a summary, or in an error the model helpfully echoes. It is simultaneously copied into the provider's request logs, into your prompt cache, into any trace exporter, and into the stored conversation you replay on the next turn — so one credential in one turn is a credential in every subsequent request of that conversation. Prompt injection makes it worse only in the sense that the attacker no longer has to wait.

The rule that follows is structural, not behavioural: the credential must live below the layer the model can read. The tool executor attaches it at the transport boundary; the tool result is filtered before it becomes context. Concretely, a tool that returns raw HTTP responses returns `Authorization`, `Cookie`, and `Set-Cookie` headers, and a "show me the request you made" debug tool returns the whole thing. Strip those in the executor. The [fail pillar](/fail/) collects the shapes this takes in production, including the token-leakage patterns that start exactly here.

### 5. Rotate with an overlap window you can compute

Rotation goes wrong in one specific way: someone replaces the credential at the resource, and everything holding the old one fails at once. The fix is that the resource must accept two credentials during the change, and that the window must stay open long enough for the last holder of the old one to finish.

AWS Secrets Manager's rotation function is the reference implementation, and it is worth naming because the ordering is not arbitrary. Secrets Manager invokes the same function four times with a `Step` of `create_secret`, `set_secret`, `test_secret`, or `finish_secret` ([rotation by Lambda function](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotate-secrets_lambda.html)), and the [four steps](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotate-secrets_lambda-functions.html) — which the documentation names `createSecret`, `setSecret`, `testSecret`, and `finishSecret` — move three staging labels between them. `createSecret` stores the new value under `AWSPENDING`, `setSecret` changes the credential at the resource to match `AWSPENDING`, `testSecret` uses the pending version for a real read, and `finishSecret` "moves the label `AWSCURRENT` from the previous secret version to this version, which also removes the `AWSPENDING` label in the same API call", after which Secrets Manager "adds the `AWSPREVIOUS` staging label to the previous version". Your clients must fetch by the `AWSCURRENT` label rather than by a version id; that indirection is what makes the swap invisible to them.

Here is the sequence in full, including the two steps that follow `finishSecret` and that the four-step contract does not cover:

1. **Create.** Generate the new credential and store it where nothing reads it — `AWSPENDING`, or a pending key id. No traffic effect. Make it idempotent: if a pending value already exists, reuse it, so a retry does not mint a third credential.
2. **Set.** Add the new credential at the resource. **Add, do not replace.** Both the old and new credential now authenticate. This is the moment the overlap window opens, and everything currently in flight is untouched.
3. **Test.** Open a *fresh* connection with the new credential and perform a real operation. Aborting here is free, because nothing has moved: every caller is still on the old credential. A test that reuses a pooled connection authenticated with the old credential proves nothing.
4. **Finish.** Promote the new credential to the one handed out — one atomic label move. New requests get the new credential; holders of the old one keep working.
5. **Drain.** Wait for the longest lifetime any outstanding old credential can have. This is a number you already know, because you set it: the credential TTL, or the maximum of the client cache TTL, the longest connection lifetime, and the longest in-flight job, whichever model fits your system.
6. **Retire.** Remove the old credential from the resource's accepted set. The window closes here, and only here does the old credential stop working.

Steps 2 and 6 are the resource's side of the window. The client's side is dual-read, and it is one rule: during the window a caller may be holding either credential, so an authentication failure has to trigger a re-read of the label and exactly one retry, not a backoff loop and not a crash. Two credentials being simultaneously valid is what makes that retry succeed; without step 2's "add, do not replace" the retry fails identically. Cap it at one retry, because a second failure means the credential is wrong rather than stale, and a loop against a rejected credential is how a rotation bug becomes a lockout.

`rotate_with_overlap.py` runs that sequence against a fake clock and prints the timeline. The overlap is explicit, the drain is exactly the 300-second credential TTL, and no check inside the window fails:

```text
    t=   20s  2. set: resource now accepts ['k1', 'k2']. OVERLAP WINDOW OPENS.
    t=   20s  in-flight k1 credential: accepted (280s left)
    t=   40s  4. finish: k2 is now the signing key. New credentials are k2-signed.
    t=   40s  in-flight k1 credential: accepted (260s left)
    t=  340s  6. retire: resource accepts ['k2']. OVERLAP WINDOW CLOSES.
```

Then it runs the reordering, doing step 6 before step 5, which is the failure this whole ordering exists to prevent:

```text
    t=   20s  retire k1 immediately, because the new key 'works'
    t=   20s  in-flight k1 credential: DENIED — signing key 'k1' is not accepted
    that credential had 280s of validity left. Every holder of one fails at once.
```

Two real-world qualifications. First, some resources accept only one credential per principal — a database user has one password — and there the overlap has to come from a second principal. That is exactly what Secrets Manager's [alternating users strategy](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotation-strategy.html) does: it clones the user and alternates which one it updates, so that "after rotation, both `user` and `user_clone` credentials are valid". AWS is candid that the single-user strategy does not get you a clean window — "there is a short period of time between when the password in the database changes and when the secret is updated. During this time, there is a low risk of the database denying calls that use the rotated credentials" — and recommends a retry strategy to absorb it. Choose knowingly.

Second, this is not exotic; you are already relying on it. EC2 instance metadata rotates the role credentials for you, and AWS documents the overlap in one sentence: "We make new credentials available at least five minutes before the expiration of the old credentials." That five minutes is the drain window, published by the issuer so that clients can refresh without a gap.

### 6. Revoke, which rotation does not do

Rotation is scheduled and gradual. Revocation is immediate and targeted, and they answer different questions. Rotation asks "how long may any credential live"; revocation asks "how do I make *this* one stop working, now". A system that can only rotate discovers during its first incident that its only lever leaves the attacker's copy valid for the whole overlap window — and if step 6 was never automated, valid indefinitely.

`rotate_with_overlap.py` demonstrates that failure directly. After a full create-set-test-finish rotation, the leaked credential is still accepted, because the old key is still in the accepted set *by design*:

```text
    t=   60s  you rotate: steps 1 through 4 complete
    t=   60s  the leaked credential: accepted (240s left)
    rotation alone bought nothing. The old key is still accepted, by design.
    t=   60s  the leaked credential, after revoke(jti): DENIED — credential revoked
```

There are only three ways to actually revoke, and it is worth knowing which one you have:

- **Delete the principal at the resource.** Vault's dynamic database credentials work this way: the credentials Vault generated exist at the database under a lease, and `vault lease revoke LEASE_ID` revokes them there. No verifier has to be consulted, because there is nothing left to authenticate as. Strongest, and available only when the credential corresponds to a real principal you created.
- **Deny at the authorizer.** AWS revokes role sessions by attaching an inline policy named `AWSRevokeOlderSessions` that denies everything when `aws:TokenIssueTime` is `DateLessThan` a timestamp ([revoke temporary credentials](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html)). Read what that means precisely: the credential still authenticates. It simply no longer authorizes anything. AWS also sets the cut-off "approximately 30 seconds into the future" to cover policy propagation, and notes you cannot revoke sessions for a service-linked role.
- **Expiry.** If the lifetime is shorter than the time it takes you to notice and respond, expiry *is* your revocation. This is the only one that works when nobody is watching, and it is why step 3 issues with a five-minute TTL.

What all three require is that the verifier consults state at request time. A pure bearer JWT checked by signature alone cannot be revoked — that is not an implementation gap, it is the design. If you issue self-contained tokens, either keep a deny list the verifier reads (the `jti` check in `issuing_broker.py`) or accept that the lifetime is the revocation. Whichever you pick, exercise the path on a normal Tuesday. A revocation mechanism first run during an incident is an untested one.

## Decision table

| Option | When it wins | Blast radius | Rotation cost | Per-user support | Audit |
| --- | --- | --- | --- | --- | --- |
| Environment variable | Single-tenant agent, one resource, every operator may already read all of it. | Every resource the credential reaches × every user in them × unbounded time. | A redeploy of every consumer, coordinated, with a window where old and new both have to work and the variable can hold only one. | None. The value is a per-process constant, so it carries the union of all entitlements. | You see that the agent acted. You cannot see for whom, because the credential does not carry a subject. |
| Long-lived secret in a vault | You need the secret out of the repo and reads audited, and cannot change the consuming code yet. | Identical to the environment variable. Storage moved; reach and lifetime did not. | Better: one write, and consumers re-read. Still needs the overlap ordering, and consumers must actually re-read rather than cache at boot. | None, unless you store one secret per user, which reproduces the storage problem at user scale. | Who read the secret, and when. Not what they did with it afterwards. |
| Short-lived issued credential | More than one user, or more than one resource, or any requirement to answer "who did this, for whom". | One resource × one subject × the TTL. Bounded without detection. | Rotation becomes key rotation, done once at the issuer, with an overlap window equal to the credential TTL. Consumers change nothing. | Native. The subject is a parameter of issuance and is carried in the credential. | Every issuance is a record of agent, subject, resource, and time — before the call, not inferred from it. |

The middle row is the one to be honest about. Moving a secret into a vault is real progress on the questions "where is it" and "who read it", and no progress at all on "what does it reach" and "how long does it live". If you stop there, say so explicitly in your design document, because the row above and the row below it look similar on an architecture diagram and differ by a factor of thousands in the arithmetic from the previous section.

The bottom row's cost is not in the table because it is not per-credential: you now operate an issuer, and it is in the request path of everything. Budget for its availability the way you budget for your database's.

## Checklist

- [ ] You can state, from the grant rather than from the code, how many resources and how many users' data the agent's current credential reaches.
- [ ] No credential the agent uses against a resource is read from an environment variable at startup and held for the life of the process.
- [ ] The agent's own identity comes from a source it cannot forge — a projected token file, IMDS, or a CI OIDC endpoint — and is re-read rather than cached past its expiry.
- [ ] Every trust policy that accepts a federated token conditions on both the audience and the subject, and the subject condition names a specific repository, branch, environment, or service account.
- [ ] Issued credentials carry a subject, a resource, and an expiry, and the verifier checks all three rather than only the signature.
- [ ] The credential TTL is a number you chose deliberately and can state, and it is shorter than your realistic detection-to-response time.
- [ ] Log redaction lives in a formatter or handler shared by every logger in the process, and a test asserts that a credential logged by a third-party library's logger does not appear in the output.
- [ ] The redactor is fed by the issuer, so every value that gets minted is a value that gets scrubbed.
- [ ] No credential is placed in a model prompt, a tool result, or a stored conversation, and tool results are stripped of `Authorization`, `Cookie`, and `Set-Cookie` headers before they become context.
- [ ] The rotation runbook adds the new credential at the resource before anything uses it, and retires the old one only after a drain period you can compute rather than estimate.
- [ ] Retiring the old credential is an automated step in the rotation job, not a follow-up ticket.
- [ ] A caller that gets an authentication failure re-reads the credential and retries exactly once, rather than looping against it or crashing the request.
- [ ] You can revoke one credential without rotating the key, and you have done it at least once outside an incident.
- [ ] `subprocess` calls pass an explicit `env=` containing only the variables the child needs.
- [ ] Offboarding a user stops new issuance for that subject, and the maximum time before their outstanding credentials stop working is the TTL you wrote down above.

## Failure modes

### Rotation completed, and the leaked credential kept working

**Symptom:** you rotate in response to a leak, the rotation job reports success, and the credential in the pastebin keeps returning data.

**Cause:** the overlap window exists precisely to keep the old credential alive, and retiring it is the step after the one your tooling automates. Secrets Manager's rotation function ends at `finishSecret`, which moves a label; nothing in the four-step contract removes the old credential at the resource, because the drain time is a property of your application that the rotation service cannot know. Steps 5 and 6 are yours, and if nobody wrote them, "rotated" means "issued a new one alongside".

**Fix:** make retirement an automated step of the rotation job, with the drain period as an explicit parameter. Then check the fix by asserting the old credential is rejected after the job completes, rather than asserting the new one is accepted. And in an incident, do not reach for rotation at all: revoke, which is a different call.

### The IAM role that any repository on GitHub could assume

**Symptom:** nothing. This one has no symptom until it is used, which is why it is worth grepping for today.

**Cause:** a role trust policy for GitHub Actions OIDC that conditions on `token.actions.githubusercontent.com:aud` and not on `:sub`. The audience value is not a secret — for the AWS integration it is `sts.amazonaws.com` — and any repository on GitHub can request a token carrying it. GitHub's own guidance is to evaluate the `sub` condition to limit which workflows can assume the role, and its example uses `"token.actions.githubusercontent.com:sub": "repo:octo-org/octo-repo:ref:refs/heads/octo-branch"` ([configuring OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)). A `StringLike` of `repo:octo-org/octo-repo:*` is narrower but still matches every branch, tag, environment, and pull request in that repository, so anything that can cause a workflow to run there can assume the role.

**Fix:** condition on `sub` with `StringEquals` against a specific ref or environment wherever you can, and treat `StringLike` with a trailing wildcard as a decision you justify in the pull request. One caveat you should verify against the current page rather than take from here: GitHub documents that repositories created after July 15, 2026 use an immutable `sub` format embedding numeric ids, of the form `repo:octo-org@123456/octo-repo@456789:ref:refs/heads/main`. A policy matching the older string form will not match the newer one, and the failure is a broken deploy rather than an insecure one — but check the format your repositories actually emit before writing the condition.

### The agent worked for an hour and then every call returned 401

**Symptom:** a freshly deployed agent runs cleanly for roughly an hour, then fails authentication on every request until it is restarted, at which point it works for another hour.

**Cause:** the workload identity token was read once at import and cached in a module-level variable. A Kubernetes projected service account token defaults to a one-hour lifetime and is refreshed *in the file* by the kubelet; a process that read the file at startup is holding a string that expired. The same shape appears with IMDS credentials cached past their `Expiration`.

**Fix:** read the token at the point of use, not at import — `read_workload_identity()` in the example does exactly that — or refresh on a timer keyed to the expiry rather than to a fixed interval you guessed. If you must cache, cache with the expiry attached and refresh before it, which is what AWS's five-minutes-early publication of new IMDS credentials is designed to let you do.

### You revoked the session and the credential still authenticated

**Symptom:** you click **Revoke active sessions** on an IAM role after a leak, and calls made with the leaked credential in the seconds around that click still succeed. The credential is still accepted as an identity afterwards; it simply gets an authorization failure rather than an authentication failure.

**Cause:** AWS implements session revocation as a deny policy, not as credential invalidation. It attaches an inline policy named `AWSRevokeOlderSessions` that denies `"Action": "*"` on `"Resource": "*"` under a `DateLessThan` condition on `aws:TokenIssueTime`, and the condition "applies the restrictions only if the user assumed the role *before* the point in time when you revoke the permissions". AWS sets that point approximately thirty seconds into the future to absorb policy propagation delay, and states plainly that "any user who assumes the role more than approximately 30 seconds after you choose Revoke active sessions is not affected". So the credential remains a valid signing key for its whole natural lifetime; what stands between it and your data is one policy, evaluated per request and subject to propagation. Edit or detach that policy later and the credential works again.

**Fix:** know which of the three revocation shapes you have, and do not treat a deny policy as destruction. Where the credential corresponds to a principal you created, delete the principal. Where it does not, treat the deny policy as a mitigation with a propagation window, and keep the session duration short so expiry closes the rest of the gap. Note that you cannot revoke sessions for a service-linked role at all, and that valid users whose sessions you revoked have to acquire new credentials — the AWS CLI caches until expiry, so `rm -r ~/.aws/cli/cache` is part of the runbook.

### The credential appeared in the model's summary of what it did

**Symptom:** a conversation transcript, a trace, or a support ticket contains a live credential that no line of your code ever logged.

**Cause:** the credential entered the model's context — as a tool result containing raw response headers, as a debug tool echoing the request it made, or as an exception message rendered into the transcript — and the model reproduced it, because reproducing its input is what it does. From there it is in the provider's request log, in the prompt cache, and in every subsequent turn of the conversation you replay.

**Fix:** move the credential below the model. The executor attaches it at the transport boundary and strips it from anything returned upward: `Authorization`, `Cookie`, and `Set-Cookie` at minimum, plus whatever your provider uses. Redact tool results before they are appended to context, not only before they are logged — those are different code paths, and only one of them is the logger. Then treat any credential that reached a context as leaked, and revoke it rather than waiting for it to expire.

### You deleted the secret from the environment and it was still there

**Symptom:** the process pops the variable from `os.environ` immediately after reading it, and a support engineer still finds the value in `/proc/PID/environ` hours later.

**Cause:** `/proc/PID/environ` reflects the environment at `execve(2)`, and later modifications are not written back to it. Deleting the key from the interpreter's copy changes the interpreter's copy.

**Fix:** stop passing secrets through the environment. If you are mid-migration and cannot yet, pass the value on a file descriptor or a unix socket the process reads and closes, so there is no copy in the environment block to begin with — and remember that the file's contents are readable by whoever can read the file, which is a different, smaller problem you can reason about with filesystem permissions.

### The rotation test passed against a connection the rotation had not touched

**Symptom:** `test_secret` passes, `finish_secret` promotes the new credential, and the first genuinely new connection afterwards fails to authenticate.

**Cause:** the test reused a pooled connection that was authenticated with the *old* credential when it was opened. Most connection protocols authenticate at connection time and never again, so a query on an existing connection proves nothing about the new credential.

**Fix:** the test step must open a new connection with the new credential explicitly and perform a real operation on it. Read-only is fine; reusing the pool is not. AWS's rotation templates test the pending version "by using read access" for exactly this reason. Note the same property is why in-flight work survives the promotion in step 4 — long-lived connections keep working — and why the drain in step 5 has to be at least as long as your maximum connection lifetime, not just your credential TTL.

## Doing this at scale

Everything above is one integration. Multiply it by every system the agent touches and the recurring work is visible: an issuer in the request path of every call, one trust chain per platform to keep correct, a rotation job per credential with a drain period somebody has to keep accurate, a revocation path per credential type, a redaction registry fed by every issuer, and an audit record that ties each issuance to an agent and the user it acted for. None of that is hard. It is just permanently yours, and it grows linearly with integrations while your team does not.

The end state worth aiming at is an agent that holds no resource credential at all. Not a short-lived one — none. The agent authenticates to a control layer with a single token, names the connection it wants, and the layer holds the credential, evaluates policy for that request, makes the call, and writes the record. Rotating the underlying database password then changes nothing in the agent, because the agent never had it. A compromise of the agent process leaks a revocable gateway token instead of a standing grant.

[Agentic Fabriq](https://agenticfabriq.com) is built as that layer: agent identity, per-request policy evaluation, credential vaulting, and an audit trail attributing every action to an agent and the user it acted for. The call shape is a named connection rather than a credential:

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        for tool in await af.list_tools():
            print(tool)

        result = await af.invoke_connection(
            "orders_db",
            method="query",
            parameters={
                "sql": "SELECT id, status FROM orders WHERE customer_id = :customer LIMIT 20",
                "params": {"customer": "amara@example.com"},
            },
        )
        for row in result.get("rows", []):
            print(row)


asyncio.run(main())
```

The runnable version is `examples/govern-credentials/fabriq_no_credential.py`. Connection names, method names, and response shapes are per-deployment, so run `afctl tools list` against your own gateway rather than copying `orders_db`, `query`, or the `rows` key on faith. The property worth evaluating is not the shorter code. It is that the blast-radius arithmetic from the top of this guide is computed by something other than you, per request, and that rotation and revocation stop being work you schedule. If you would rather own that yourself, everything above still holds — a [control layer like Agentic Fabriq](https://agenticfabriq.com) changes who operates the issuer, not what a correct issuer does.

## Further reading

The [govern pillar](/govern/) is the map for the rest of this: OAuth flows for the delegated case, least-privilege scope design, and audit trails that make issuance answerable rather than inferable. The [connect pillar](/connect/) shows the same credential lifecycle from the integration side, and [connecting an agent to a database](/connect/databases/) covers the row-level enforcement that stops being optional once you accept that authorization has moved out of your query code. The [fail pillar](/fail/) collects the incidents, including the token-leakage patterns that begin with a credential in a context window.

Primary sources for everything asserted above:

- [proc_pid_environ(5)](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html) — the environment file's contents, its `PTRACE_MODE_READ_FSCREDS` access check, and the fact that it reflects only the environment at `execve(2)`.
- [Use the Instance Metadata Service](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-v2-how-it-works.html) — the IMDSv2 `PUT`/`GET` header names, the six-hour maximum session TTL, the `X-Forwarded-For` rejection, and the default hop limit of 1.
- [Retrieve security credentials from instance metadata](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-security-credentials.html) — the `iam/security-credentials/`*role-name* path, the SSRF warning, and the five-minutes-early publication of replacement credentials.
- [IAM roles for Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html) — one role per instance, and instance identity roles.
- [Projected volumes](https://kubernetes.io/docs/concepts/storage/projected-volumes/) — the `serviceAccountToken` fields, the one-hour default, the 600-second minimum, and `--service-account-max-token-expiration`.
- [Managing service accounts](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/) — bound token lifetimes, invalidation on pod deletion, and kubelet refresh.
- [OpenID Connect in GitHub Actions](https://docs.github.com/en/actions/reference/security/oidc) — `id-token: write`, the two `ACTIONS_ID_TOKEN_REQUEST_*` variables, the issuer, and the `sub` claim formats including the immutable form.
- [Configuring OpenID Connect in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws) — the trust policy shape and the recommendation to evaluate the `sub` condition.
- [AssumeRoleWithWebIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html) — `DurationSeconds` bounds and default, and the identity-token error codes.
- [Workload identity federation with other clouds](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds) — the `sts.googleapis.com/v1/token` exchange, the `grant_type`, and the audience resource name format.
- [IAM Roles Anywhere](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html) and [CreateSession](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/authentication-create-session.html) — trust anchors, profiles, the account trust boundary, and the session duration arithmetic.
- [Rotation by Lambda function](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotate-secrets_lambda.html) and [Lambda rotation functions](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotate-secrets_lambda-functions.html) — the four `Step` values and what each staging label does.
- [Lambda function rotation strategies](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotation-strategy.html) — single-user versus alternating-users, and the window each leaves.
- [Revoke IAM role temporary security credentials](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html) — `AWSRevokeOlderSessions`, the `aws:TokenIssueTime` condition, and the thirty-second forward window.
- [Vault database secrets engine](https://developer.hashicorp.com/vault/docs/secrets/databases) — dynamic per-request database users, leases, and `vault lease revoke`.
- [Python logging flow](https://docs.python.org/3/howto/logging.html#logging-flow) — where logger filters and handler filters sit relative to propagation.

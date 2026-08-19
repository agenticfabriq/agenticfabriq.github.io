---
title: OAuth Flows for AI Agents
description: The three OAuth grants an AI agent actually needs, why RFC 8693 token exchange is the one most teams skip, and how to scope a downstream token down.
pillar: govern
updated: "2026-08-18"
keywords:
  - oauth for ai agents
  - token exchange rfc 8693
  - client credentials agent
  - pkce s256
  - refresh token rotation
  - audience restriction
relatedGuides:
  - govern/delegated-identity
  - govern/credentials
  - fail/over-scoped-oauth
howTo:
  - name: Read the authorization server's metadata before writing any code
    text: Fetch the RFC 8414 metadata document and check code_challenge_methods_supported and grant_types_supported instead of hardcoding endpoint paths.
  - name: Generate a code verifier and an S256 code challenge
    text: Take 32 random octets, base64url-encode them for the code_verifier, and send BASE64URL-ENCODE(SHA256(ASCII(code_verifier))) as the code_challenge.
  - name: Send the user to the authorization endpoint
    text: Request response_type=code with code_challenge_method=S256 and a state value you compare in constant time when the redirect returns.
  - name: Redeem the code and read the scope you were actually granted
    text: Post grant_type=authorization_code with the code_verifier, then read the scope member of the response rather than assuming the user granted everything.
  - name: Record the consent as an artifact you can show back
    text: Store the granted scope, the issuer, the timestamp, and the client id together, because OAuth defines no consent receipt for you.
  - name: Exchange the user's token for a narrower downstream token
    text: Post grant_type=urn:ietf:params:oauth:grant-type:token-exchange with subject_token, subject_token_type, audience, and scope naming one target service.
  - name: Verify the exchanged token is narrower before you use it
    text: Compare the issued scope against the subject token's scope and reject the result if the exchange added authority, because RFC 8693 does not forbid that.
  - name: Refresh with rotation and treat a reuse error as a breach
    text: Discard the old refresh token the moment a new one arrives, and page a human when the server rejects a refresh token you believe is current.
faq:
  - question: Which OAuth flow should an AI agent use?
    answer: Use the authorization code grant with PKCE when the agent acts for a named person, client credentials when the agent acts as itself with no user involved, and RFC 8693 token exchange when the agent already holds a token and needs a narrower one for a downstream call.
  - question: What is OAuth token exchange and when does an agent need it?
    answer: Token exchange is the RFC 8693 grant `urn:ietf:params:oauth:grant-type:token-exchange`, which trades one security token for another. An agent needs it when it holds a user's access token and must call a downstream service that should receive only a fraction of what the user granted.
  - question: Should confidential clients use PKCE?
    answer: Yes. RFC 9700 section 2.1.1 says PKCE is RECOMMENDED for confidential clients and MUST be used by public clients, and notes the advice applies to all kinds of OAuth clients including web applications. Use the S256 method; plain offers no protection against an attacker who can read the authorization request.
  - question: Does RFC 8693 token exchange guarantee the new token is narrower?
    answer: No. RFC 8693 defines a general security token service and lets the authorization server issue a token with rights the subject token never carried. Downscoping is a policy you configure and verify, not a property of the grant.
  - question: What does refresh token reuse detection actually catch?
    answer: It catches a stolen refresh token. With rotation, each refresh invalidates the previous token, so if both an attacker and the legitimate client refresh, one presents an invalidated token and the authorization server learns of the breach. RFC 9700 section 4.14.2 requires rotation or sender-constraining for public clients.
  - question: Is the OAuth implicit flow still safe to use?
    answer: No. RFC 9700 section 2.1.2 says clients SHOULD NOT use the implicit grant, because access tokens returned in the authorization response are exposed to leakage and replay and there is no standardized way to sender-constrain them.
---

## TL;DR

- Three grants matter for agents: `authorization_code` with PKCE ([RFC 7636](https://www.rfc-editor.org/rfc/rfc7636#section-4.2)), `client_credentials` ([RFC 6749 section 4.4.2](https://www.rfc-editor.org/rfc/rfc6749#section-4.4.2)), and `urn:ietf:params:oauth:grant-type:token-exchange` ([RFC 8693 section 2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.1)). RFC 9700 says clients SHOULD NOT use the implicit grant and MUST NOT use the password grant; treat both as off the menu.
- Token exchange is the one most teams have never used and most agent systems need. An agent holding a user's token and calling a downstream service should exchange that token for one scoped to that service, not forward the original.
- Forwarding the original hands every downstream hop the union of everything the user consented to, and makes the audit trail say the user did it. Both problems are the same mistake.
- PKCE with `code_challenge_method=S256` belongs on every client. [RFC 9700 section 2.1.1](https://www.rfc-editor.org/rfc/rfc9700#section-2.1.1) makes it mandatory for public clients and RECOMMENDED for confidential ones, and says the advice applies to web applications too.
- Refresh token rotation with reuse detection is the only control in ordinary OAuth that tells you a refresh token was stolen. [RFC 6749 section 10.4](https://www.rfc-editor.org/rfc/rfc6749#section-10.4) describes the mechanism; [RFC 9700 section 4.14.2](https://www.rfc-editor.org/rfc/rfc9700#section-4.14.2) makes it or sender-constraining mandatory for public clients.

## Who this is for

You are building an agent that calls more than one system, and at least one of those calls happens because a person authorized it. This guide covers the three grant types that survive current security guidance, the exact wire parameters for each, and the flow that turns a broad user grant into a narrow downstream one. Skip it if your agent talks to exactly one API with a static API key and no human is delegating anything — you have a secrets management problem, not an OAuth problem.

## The problem

An agent gets a token from a user and then keeps using it. That sentence is the whole failure.

Here is the concrete shape. A user consents to an agent with `invoices:read`, `payments:write`, and `contacts:read`. The agent needs to enrich an invoice, so it calls an internal enrichment service, and it does the obvious thing: copies the `Authorization: Bearer` header it received straight into the outbound request. The enrichment service now holds a token that can move money. It did not ask for that, it has no idea it has it, and its request logs — which are not treated as secret, because why would they be — now contain a credential that writes payments. [RFC 9700 section 4.9.2](https://www.rfc-editor.org/rfc/rfc9700#section-4.9.2) describes this precisely: an attacker who compromises a resource server "would also be able to obtain other access tokens held on the compromised system that would potentially be valid to access other resource servers." A token that every service accepts is a token that every compromised service can replay.

The second half of the damage is quieter. The enrichment service sees a token whose subject is the user. Every log line it writes attributes the call to a human who has never heard of it. When someone later asks which agent read which record, the answer in your logs is a person's name, and there is no field anywhere that says an agent was in the middle. You cannot revoke the agent without revoking the user, and you cannot tell the two apart after the fact.

Then there is the stale menu. Most engineers carry two flows in their head — authorization code and client credentials — and reach for whichever is closer. Two more still appear in tutorials and both are finished. The implicit grant returns an access token in the authorization response, where it lands in the URL and in browser history; [RFC 9700 section 2.1.2](https://www.rfc-editor.org/rfc/rfc9700#section-2.1.2) says clients SHOULD NOT use it, and adds that no standardized method for sender-constraining exists for tokens issued that way, so a leaked one can simply be replayed. The resource owner password credentials grant takes the user's actual password and hands it to the client; [RFC 9700 section 2.4](https://www.rfc-editor.org/rfc/rfc9700#section-2.4) says it MUST NOT be used, because it spreads credentials to more places than the authorization server and trains users to type their password into whatever asks. It also cannot express a second factor. For an agent this is worse than for a web app: an agent that holds a password can re-authenticate forever, and there is nothing for the user to revoke short of a password change.

## Step by step

Before the code, the mapping that decides everything else. Each grant answers a different question:

- **`authorization_code`** answers *the agent acts for a person who consented*. A human sees a consent screen, and the resulting token carries their identity. Add PKCE, always.
- **`client_credentials`** answers *the agent acts as itself*. No user, no consent screen, no delegation. [RFC 6749 section 4.4.2](https://www.rfc-editor.org/rfc/rfc6749#section-4.4.2) sets `grant_type` to the literal `client_credentials`, section 4.4 restricts the grant to confidential clients, and [section 4.4.3](https://www.rfc-editor.org/rfc/rfc6749#section-4.4.3) says "A refresh token SHOULD NOT be included" — the client can simply authenticate again. Use it for the agent's own housekeeping: reading its own configuration, writing its own telemetry, polling a queue that belongs to it.
- **Token exchange** answers *the agent received one token and needs a different, narrower one*. It lets an agent call a second service without handing over everything the first one gave it.

The path below runs authorization code with PKCE end to end and then exchanges the result. It targets Keycloak, which implements the standard token exchange and starts in one command in dev mode — see [Keycloak's getting-started page](https://www.keycloak.org/getting-started/getting-started-docker) for the exact invocation. Any authorization server that publishes discovery metadata and supports the exchange grant will do; the provider-specific claims below are marked as such. Every parameter is spelled the way its RFC spells it. Runnable sources: `examples/govern-oauth-flows/pkce_authorization_code.py`, `examples/govern-oauth-flows/token_exchange.py`, and the shared HTTP helpers in `examples/govern-oauth-flows/oauth_http.py`.

### 1. Ask the authorization server what it supports

Do not hardcode endpoint paths. [RFC 8414 section 3](https://www.rfc-editor.org/rfc/rfc8414#section-3) defines a metadata document that names them, and reading it tells you two things you need before you write anything else: whether the server advertises `S256`, and whether it advertises the token exchange grant.

There is a subtlety worth getting right, because most code gets it wrong. RFC 8414 inserts `/.well-known/oauth-authorization-server` **between the host and the issuer's path**. OpenID Connect Discovery appends `/.well-known/openid-configuration` to the issuer instead. For an issuer with no path component the two coincide; for anything path-scoped — every Keycloak realm, for one — they do not.

```python
def metadata_urls(issuer: str) -> list[str]:
    parts = urllib.parse.urlsplit(issuer.rstrip("/"))
    path = parts.path
    return [
        urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, f"/.well-known/oauth-authorization-server{path}", "", "")
        ),
        urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, f"{path}/.well-known/openid-configuration", "", "")
        ),
    ]
```

[RFC 8414 section 3.3](https://www.rfc-editor.org/rfc/rfc8414#section-3.3) requires the `issuer` value inside the document to be identical to the issuer you built the URL from, and says that if they are not identical, the data "MUST NOT be used". That check is three lines and it is the only thing standing between a mistyped issuer and an agent that trusts endpoints someone else published, so write it.

Two members carry defaults that will mislead you. `code_challenge_methods_supported` is OPTIONAL, so its absence is not evidence that PKCE is unsupported — RFC 9700 section 2.1.1 requires servers to support PKCE regardless. And `grant_types_supported` defaults to `["authorization_code", "implicit"]` when omitted, which tells you nothing about extension grants. A list that is present and omits the exchange grant is real information; an absent list is not.

### 2. Generate the code verifier and the S256 challenge

[RFC 7636 section 4.1](https://www.rfc-editor.org/rfc/rfc7636#section-4.1) defines `code_verifier` as a high-entropy random string of 43 to 128 characters drawn from the unreserved set `[A-Z] / [a-z] / [0-9] / "-" / "." / "_" / "~"`, and recommends generating a 32-octet sequence and base64url-encoding it, which produces exactly 43 characters. [Section 4.2](https://www.rfc-editor.org/rfc/rfc7636#section-4.2) defines the transformation for the `S256` method as `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`, and states that a client capable of `S256` MUST use it.

```python
def make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
```

The other registered method is `plain`, where `code_challenge = code_verifier`. It exists for clients that cannot compute SHA-256, which in 2026 is no client you are writing. Sending the verifier in the authorization request means anyone who can read that request has the verifier, and PKCE stops protecting anything; RFC 9700 section 2.1.1 says clients SHOULD use methods that do not expose the verifier and notes `S256` is currently the only one. The example refuses to downgrade rather than falling back.

Now the attack this defeats, mechanically. [RFC 7636 section 1](https://www.rfc-editor.org/rfc/rfc7636#section-1) calls it the authorization code interception attack. The authorization code comes back to the client over the redirect, and on a mobile or desktop platform that last hop is not TLS — it is a custom URI scheme or a loopback address handled by the operating system. A malicious app that registers itself as a handler for the same scheme receives the code, and because the code alone was enough to get a token, it gets the token. PKCE breaks the chain by making the code redeemable only by whoever knows the verifier, which never leaves the process that generated it. [Section 4.6](https://www.rfc-editor.org/rfc/rfc7636#section-4.6) specifies what the server does with it: recompute the challenge from the received `code_verifier`, compare it to the stored `code_challenge`, and return `invalid_grant` if they differ.

That is why the "PKCE is only for public clients" habit is wrong. The verifier is not a secret substitute — it is a per-transaction binding between the authorization request and the token request. A confidential web app has a client secret and still cannot prove that the code it is redeeming came from the browser session it started. PKCE proves exactly that, which is why RFC 9700 section 2.1.1 lists it as RECOMMENDED for confidential clients and adds the note that the advice applies to all kinds of OAuth clients, including web applications.

### 3. Send the user through the authorization endpoint

```python
params = {
    "response_type": "code",
    "client_id": client_id,
    "redirect_uri": REDIRECT_URI,
    "scope": scope,
    "state": state,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
}
url = f"{metadata['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
```

`state` is a separate control from PKCE and both belong here. Generate it from a CSRF-safe source and compare it with `secrets.compare_digest` when the redirect returns; [RFC 6749 section 10.12](https://www.rfc-editor.org/rfc/rfc6749#section-10.12) is the reason. A locally run agent should use a loopback redirect on `127.0.0.1`, which [RFC 8252 section 7.3](https://www.rfc-editor.org/rfc/rfc8252#section-7.3) covers; a server-side agent uses an HTTPS redirect URI it controls.

Failure at this endpoint does not look like an HTTP error. [RFC 6749 section 4.1.2.1](https://www.rfc-editor.org/rfc/rfc6749#section-4.1.2.1) has the authorization server redirect back with an `error` parameter instead of a `code`. Code that reads `response["code"]` without checking for `error` first crashes on the most ordinary outcome there is: a user clicking Cancel.

### 4. Redeem the code, and read what you were actually granted

```python
form = {
    "grant_type": "authorization_code",
    "code": code,
    "redirect_uri": REDIRECT_URI,
    "client_id": client_id,
    "code_verifier": verifier,
}
```

[RFC 6749 section 5.1](https://www.rfc-editor.org/rfc/rfc6749#section-5.1) is the response, and one detail in it decides whether your agent is over-scoped. `scope` is "OPTIONAL, if identical to the scope requested by the client; otherwise, REQUIRED." Read that carefully in both directions: an absent `scope` means you got exactly what you asked for, but a present one does not mean you did not — a server is free to echo an identical scope. So the only safe move is to diff whatever you got against what you requested, and to treat absence as equality rather than as silence. Users decline individual permissions, admins restrict them at the tenant, and an agent that assumes the full set will fail later at an API call rather than now at a place where you can say something useful.

`expires_in` is RECOMMENDED, not REQUIRED, so it can legitimately be absent. Treat that as "expiry unknown, refresh proactively", not as "never expires".

### 5. Record the consent as an artifact

OAuth does not define a consent receipt. There is no standard endpoint that answers "what did this user agree to, and when". The only normative evidence you get is the `scope` member of that token response, and it is a fact about a moment in time that nothing preserves for you.

So preserve it yourself, at the moment of grant, as its own record: the issuer, the client id, the resolved user identifier, the scopes requested, the scopes granted, the timestamp, and the redirect URI in force. That set is enough to reconstruct the sentence a user or an auditor will ask you to produce — *on 14 March, this person authorized this agent to read invoices and nothing else* — and it is enough to render a settings page that shows them the same sentence and offers a revoke button next to it. The revoke button calls the [RFC 7009 section 2.1](https://www.rfc-editor.org/rfc/rfc7009#section-2.1) revocation endpoint with `token` and `token_type_hint=refresh_token`.

Two things about that record are worth stating plainly, because they are convention rather than protocol. First, re-consent must overwrite it, not append silently; a stale record showing a scope the user has since narrowed is worse than no record. Second, the record is not the authority — the authorization server is. If your record says `invoices:read` and the token carries more, believe the token and treat the difference as an incident.

### 6. Exchange the user's token for a narrower downstream token

This is the step the guide exists for. The agent holds a token good for everything the user granted, and it is about to call one downstream service that needs a fraction of that. [RFC 8693 section 2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.1) is the mechanism, and its parameters are exact:

| Parameter | Status in RFC 8693 section 2.1 | What it carries |
| --- | --- | --- |
| `grant_type` | REQUIRED | The literal `urn:ietf:params:oauth:grant-type:token-exchange` |
| `subject_token` | REQUIRED | The token representing the party on whose behalf the request is made |
| `subject_token_type` | REQUIRED | A token type identifier for `subject_token` |
| `actor_token` | OPTIONAL | A token representing the acting party — the agent itself |
| `actor_token_type` | REQUIRED when `actor_token` is present, MUST NOT be included otherwise | A token type identifier for `actor_token` |
| `audience` | OPTIONAL | The logical name of the target service. May repeat |
| `resource` | OPTIONAL | An absolute URI for the target service, no fragment. May repeat |
| `scope` | OPTIONAL | Space-delimited scopes wanted on the issued token |
| `requested_token_type` | OPTIONAL | The type of token you want back |

The token type identifiers are URIs, defined in [RFC 8693 section 3](https://www.rfc-editor.org/rfc/rfc8693#section-3), and they are spelled exactly like this:

```text
urn:ietf:params:oauth:token-type:access_token
urn:ietf:params:oauth:token-type:refresh_token
urn:ietf:params:oauth:token-type:id_token
urn:ietf:params:oauth:token-type:saml1
urn:ietf:params:oauth:token-type:saml2
urn:ietf:params:oauth:token-type:jwt
```

The last one is defined in RFC 7519 section 9 rather than in RFC 8693, and RFC 8693 draws a distinction worth keeping: `...:access_token` means "a typical OAuth access token from this authorization server, opaque to you", while `...:jwt` means "specifically a JWT". An access token may happen to be a JWT; the client is not supposed to care.

The request itself, on the wire:

```http
POST /realms/agents/protocol/openid-connect/token HTTP/1.1
Host: auth.example.com
Authorization: Basic ZXhjaGFuZ2UtY2xpZW50OnNlY3JldA==
Content-Type: application/x-www-form-urlencoded

grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange
&subject_token=eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
&subject_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token
&requested_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token
&audience=enrichment-service
&scope=invoices%3Aread
```

And in Python, with the repeated-parameter case handled — `audience` and `resource` may both appear more than once, which a dict cannot express:

```python
form: list[tuple[str, str]] = [
    ("grant_type", "urn:ietf:params:oauth:grant-type:token-exchange"),
    ("subject_token", subject_token),
    ("subject_token_type", "urn:ietf:params:oauth:token-type:access_token"),
    ("requested_token_type", "urn:ietf:params:oauth:token-type:access_token"),
]
if audience:
    form.append(("audience", audience))
if resource:
    form.append(("resource", resource))
if scope:
    form.append(("scope", scope))
```

`audience` and `resource` do the same job differently. `resource` is an absolute URI naming where you will use the token — typically the https URL of the service — and it is the same parameter [RFC 8707 section 2](https://www.rfc-editor.org/rfc/rfc8707#section-2) defines for ordinary authorization and token requests, where it says the authorization server SHOULD audience-restrict the issued token to the resources named. `audience` is a logical name that both client and server must agree on; RFC 8693 gives an OAuth client identifier, a SAML entity identifier, and an OpenID Connect issuer identifier as examples. Which one your server wants is a deployment fact, not a spec fact: Keycloak's documentation states it supports `audience` and "does not yet have support for the `resource` parameter", and expects the `audience` value to be a `client_id`. Check yours before you guess.

Ask for one target. [RFC 8693 section 2.1.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.1.1) explains why: the requested rights are "the Cartesian product of all the scopes at all the target services", so each extra audience multiplies what you are asking for and lowers the odds the server will issue anything at all. When it refuses on that ground the error code is `invalid_target`, defined in [section 2.2.2](https://www.rfc-editor.org/rfc/rfc8693#section-2.2.2).

The response, per [section 2.2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.2.1), carries `access_token` (REQUIRED, and the name is historical — the issued token "need not be an OAuth access token"), `issued_token_type` (REQUIRED), and `token_type` (REQUIRED). Read `issued_token_type` rather than assuming: it declares the representation you got back, which is not necessarily what you asked for. And `token_type` takes the literal value `N_A` when the issued token is not usable as an access token — you will see that if you request an ID token.

There is one more parameter, and it is the reason this section belongs in a governance guide rather than a protocol reference. `actor_token` carries the identity of the acting party, and when it is present the authorization server may mint a composite token: [RFC 8693 section 1.1](https://www.rfc-editor.org/rfc/rfc8693#section-1.1) draws the line between *impersonation*, where the acting party "is indistinguishable from B in that context", and *delegation*, where "principal A still has its own identity separate from B" and it is understood that A is acting for B. Forwarding a user's token is impersonation, and it is why your downstream logs say a human did it. Token exchange with an actor token is delegation, and it is the difference between a log line that names a person and one that names an agent acting for a person. In a JWT that shows up as the `act` claim, defined in [RFC 8693 section 4.1](https://www.rfc-editor.org/rfc/rfc8693#section-4.1), which nests to record a chain of actors and which consumers "MUST only consider the token's top-level claims and the party identified as the current actor" when making access decisions.

Check what your server actually does with `actor_token` before you build on it, because this is the least evenly implemented part of RFC 8693. Keycloak — the server this walkthrough targets — documents support for impersonation and *experimental* support for delegation, behind a `token-exchange-delegation` feature flag that also requires `parameterized-scopes`, and its own documentation says not to use it in production. Its delegation model is also not the one above: rather than the actor presenting an `actor_token`, the subject pre-authorizes a named actor and Keycloak records that as the `may_act` claim of [RFC 8693 section 4.4](https://www.rfc-editor.org/rfc/rfc8693#section-4.4). So `actor_token` is the right thing to reach for and the spec is unambiguous about what it means, but on a given server today you may get impersonation semantics whatever you send. The example prints the `act` claim of the issued token precisely so you can see which one you got.

### 7. Verify the exchanged token is actually narrower

Nothing in RFC 8693 says the issued token must carry fewer rights than the subject token. The specification defines a general security token service, and an authorization server is free to issue a token with scopes the subject token never had. Keycloak states this outright — token exchange there can request extra scopes not present in the initial `subject_token` unless you attach the `downscope-assertion-grant-enforcer` client policy executor — and its `audience` parameter filters audiences down while its `scope` parameter can add optional client scopes on the way up.

So check, in the client, on every exchange:

```python
def assert_narrower(response, subject_scopes: set[str], requested_scope: str | None) -> set[str]:
    if "scope" in response:
        issued = set(response["scope"].split())
    elif requested_scope:
        # RFC 8693 section 2.2.1: `scope` is OPTIONAL only when it is identical
        # to what was requested, so absence means it was honoured exactly.
        issued = set(requested_scope.split())
    else:
        # That inference needs something to be identical *to*. Request no scope
        # and an absent `scope` carries no information, so there is nothing to
        # verify -- and an unverifiable exchange is not a verified-narrow one.
        raise NotNarrower("cannot verify narrowing: send an explicit `scope`")

    gained = sorted(issued - subject_scopes)
    if gained:
        raise NotNarrower(f"exchange added scopes absent from the subject token: {', '.join(gained)}")
    return issued
```

The `else` branch is the one worth dwelling on, because the obvious implementation returns an empty set there and passes. An empty set minus anything is empty, so the check reports success on precisely the request that told it nothing — and the token you just waved through may still carry the user's entire grant. Send a `scope` on every exchange, and make the absence of one an error rather than a default.

Then check the audience, because scope alone does not stop replay. [RFC 9700 section 2.3](https://www.rfc-editor.org/rfc/rfc9700#section-2.3) says access tokens SHOULD be audience-restricted to a specific resource server, or failing that to a small set, and puts the enforcement duty on the resource server: it "is obliged to verify, for every request, whether the access token sent with that request was meant to be used for that particular resource server. If it was not, the resource server MUST refuse to serve the respective request." For JWT access tokens, [RFC 9068 section 4](https://www.rfc-editor.org/rfc/rfc9068#section-4) states the rule as a hard requirement on the receiving side: the token "MUST be rejected if `aud` does not contain a resource indicator of the current resource server as a valid audience."

That is the replay problem from The problem section, closed from the receiving end: [RFC 9700 section 4.9.3](https://www.rfc-editor.org/rfc/rfc9700#section-4.9.3) lists audience restriction as a countermeasure specifically to "prevent replay of captured access tokens on other resource servers". Blast radius is a design parameter, and this is the knob.

### 8. Refresh with rotation, and treat reuse as a breach

[RFC 6749 section 6](https://www.rfc-editor.org/rfc/rfc6749#section-6) sets `grant_type` to `refresh_token` and permits a `scope` parameter that "MUST NOT include any scope not originally granted by the resource owner". A refresh can narrow a grant; it can never widen one.

The same section says the authorization server MAY issue a new refresh token, "in which case the client MUST discard the old refresh token and replace it with the new refresh token". That MUST is on you, and it is where clients go wrong: a client that writes the new token but keeps the old one as a fallback will eventually present the old one and trip the alarm it was supposed to help ring.

The alarm is the point. [RFC 6749 section 10.4](https://www.rfc-editor.org/rfc/rfc6749#section-10.4) describes rotation as a way to detect refresh token abuse: a new refresh token is issued with every refresh, the previous one is invalidated but retained, and "if a refresh token is compromised and subsequently used by both the attacker and the legitimate client, one of them will present an invalidated refresh token, which will inform the authorization server of the breach." That is the only mechanism in ordinary OAuth that surfaces a stolen refresh token at all. Without it, a copied refresh token works quietly for as long as the grant lives, and nothing anywhere ever notices.

Note the strength of the language, because this is where blog posts overstate. RFC 6749 offers rotation as an example of what a server "could employ". [RFC 9700 section 4.14.2](https://www.rfc-editor.org/rfc/rfc9700#section-4.14.2) hardens it, and note exactly how far: authorization servers "MUST utilize one of these methods to detect refresh token replay by malicious actors for public clients" — sender-constrained refresh tokens or rotation. The mandate stops at public clients. For confidential clients neither document mandates rotation; RFC 6749 already binds the refresh token to the authenticated client. Plenty of servers rotate for every client type regardless, which is a deployment choice rather than a requirement — so do not infer from "my provider rotates" that rotation is mandated, and write your client to survive a rotating server and a non-rotating one without configuration.

What a well-behaved client does when a refresh fails:

- **`invalid_grant`** means the grant is gone. [RFC 6749 section 5.2](https://www.rfc-editor.org/rfc/rfc6749#section-5.2) defines it to cover a refresh token that is "invalid, expired, revoked, does not match the redirection URI used in the authorization request, or was issued to another client". None of those are retryable. Stop, delete the stored token, mark the connection as needing consent, and tell the user.
- **`invalid_grant` on a token you believe is current** is different, and it is the one to escalate. If you rotated correctly and stored the newest value, the server rejecting it means someone else already spent it. That is reuse detection firing, and the correct response is to revoke the whole grant and page a human, not to start a fresh consent flow as though nothing happened.
- **`invalid_client`** is your problem, not the user's — bad credentials or the wrong authentication method. Do not send the user through consent to fix it.
- **A 5xx or a timeout** is retryable with backoff. Nothing else in this list is.

Token lifetime is the design decision hiding under all of this, and it has no correct answer in any RFC. A short access token limits the window a leaked one is useful for, and forces a round trip to the authorization server often enough that revocation actually takes effect; RFC 8693's own worked example issues a 60-second token for a backend call. A long one cuts latency and load, and it means a revoked grant keeps working until the token expires, because revoking a grant does not reach into tokens already issued. State it as that trade and pick per token. Minutes for a token minted by an exchange, because revocation does not reach those at all — the revocation failure mode below has the mechanism. An hour for a user-facing access token. And a refresh token whose life is bounded by client inactivity, which RFC 9700 section 4.14.2 says SHOULD happen with the expiry left to the authorization server's discretion.

## Decision table

| Flow | Who the token represents | Consent required | What revocation does | Where it breaks |
| --- | --- | --- | --- | --- |
| `authorization_code` + PKCE | A named human. The agent acts for them. | Interactive, per user, per scope set. The user can see and withdraw it. | Revoking the grant kills the refresh token, and per [RFC 7009 section 2.1](https://www.rfc-editor.org/rfc/rfc7009#section-2.1) the server SHOULD also invalidate access tokens from the same grant. | Storage and lifecycle. One refresh token per user per agent, each of which is a standing grant on a person's data. |
| `client_credentials` | The agent itself. No user in the picture. | None. There is nobody to ask. | Rotating or deleting the client secret is the only lever, and it is all-or-nothing for that client. | Attribution. Every action looks like "the service did it", so per-user accountability has to come from somewhere else. |
| Token exchange (RFC 8693) | The subject, optionally annotated with the actor. Delegation rather than impersonation. | Inherited from the subject token. The exchange itself shows the user nothing. | Not cascaded. Section 2.1 says the exchange creates no tight linkage, so the issued token outlives the subject token's revocation unless you keep it short. | Server support and policy. The grant is optional, spellings differ, and nothing makes the result narrower unless you configure it. |

The rule of thumb: if a human authorized it, start with authorization code; if nobody did, use client credentials and accept that you owe an audit trail from elsewhere; and the moment a token crosses a service boundary inside your own estate, exchange it rather than forward it.

## Checklist

- [ ] Every authorization request carries `code_challenge_method=S256`, and the client refuses to fall back to `plain`.
- [ ] The `code_verifier` comes from a CSPRNG, is 43 to 128 characters, and never leaves the process that generated it.
- [ ] `state` is generated per request and compared with a constant-time comparison on the redirect.
- [ ] The redirect handler checks for an `error` parameter before it reads `code`.
- [ ] The token response's `scope` is diffed against the requested scope, and a partial grant is surfaced rather than discovered at the first API call.
- [ ] A consent record exists per grant, holding issuer, client id, user, requested scope, granted scope, and timestamp — and the user can see it.
- [ ] No `Authorization: Bearer` header received by a service is ever copied into an outbound request to another service.
- [ ] Every downstream call uses a token obtained by exchange, carrying an `audience` or `resource` naming exactly one target.
- [ ] The client asserts the exchanged token's scope is a subset of the subject token's scope, and fails closed both when it is not and when the response gives it nothing to compare.
- [ ] Resource servers validate `aud` on every request and reject tokens minted for someone else.
- [ ] A rotated refresh token replaces the old one atomically, and the old value is not retained as a fallback.
- [ ] `invalid_grant` on a refresh token you believe is current raises an alert, not a re-consent prompt.
- [ ] `grep` for `grant_type=password` and `response_type=token` runs in CI and fails the build.
- [ ] Offboarding calls the RFC 7009 revocation endpoint and deletes the stored tokens, in that order.

## Failure modes

### The downstream service ends up able to do more than the agent was asked to do

**Symptom:** no error. An audit turns up an internal service whose request logs contain tokens that can write to systems that service does not talk to, and the calls in your provider's audit trail are attributed to end users rather than to the agent.

**Cause:** token pass-through. The agent forwarded the `Authorization` header it received instead of exchanging it, so the downstream received the full user grant and the user's identity along with it.

**Fix:** exchange at the boundary. Every hop that crosses a service boundary gets its own token with its own `audience` and its own reduced `scope`, and the agent's identity travels as `actor_token` so the resulting token expresses delegation rather than impersonation — subject to the server-support caveat in step 6, so verify the `act` claim rather than assuming it. Then make the pass-through impossible to reintroduce: a shared HTTP client that refuses to send an inbound bearer token onward is a twenty-line change that holds the line better than a code review guideline.

### The exchange returns a token with more authority than the one you handed in

**Symptom:** the exchange succeeds, and the new token works on endpoints the original could not reach.

**Cause:** you assumed RFC 8693 means "downscope". It does not — it means "exchange". The authorization server decides what to issue, and the default in at least one widely deployed server is that the `scope` parameter can add optional client scopes the subject token never carried.

**Fix:** two layers. Server side, turn on whatever downscoping enforcement your provider offers — step 7 names Keycloak's. Client side, compare issued scope to subject scope on every exchange and fail closed, as `assert_narrower` in `examples/govern-oauth-flows/token_exchange.py` does. Do not rely on either alone: the server-side control is a configuration that can be turned off by someone who does not know why it was on.

### The exchange fails with `invalid_request` and no useful detail

**Symptom:** every token exchange returns HTTP 400 with `{"error": "invalid_request"}`, sometimes with an `error_description` that only says the subject token was rejected.

**Cause:** [RFC 8693 section 2.2.2](https://www.rfc-editor.org/rfc/rfc8693#section-2.2.2) routes several distinct problems into one code: a malformed request, an invalid `subject_token`, and a `subject_token` that is "unacceptable based on policy" all produce `invalid_request`. The most common policy rejection is deployment-specific — Keycloak requires the requesting client to appear in the subject token's `aud` claim, except when a client exchanges a token issued to itself, and refuses public clients outright.

**Fix:** work down the list before you touch the code. Confirm `subject_token_type` is the exact URN and not a bare `access_token`. Confirm the exchanging client is confidential and authenticated. Confirm the subject token names that client as an audience. Only then suspect the request shape. And log `error_description` — [RFC 6749 section 5.2](https://www.rfc-editor.org/rfc/rfc6749#section-5.2) makes it OPTIONAL, so it may be empty, but when it is populated it usually names the actual problem.

### `invalid_target` on a request that looks correct

**Symptom:** the exchange fails with `invalid_target` even though every audience you named exists.

**Cause:** you asked for too much at once — the Cartesian product problem from step 6. The request is atomic, so one unavailable target sinks all of it. Keycloak's documentation works an example where a request naming two audiences is rejected because the user holds no role at the second.

**Fix:** one audience per exchange, and the narrowest scope that completes the call. If an agent step genuinely calls three services, do three exchanges. They are cheap, and each resulting token has a blast radius of one service.

### A refresh token was stolen a month ago and nothing noticed

**Symptom:** you find out from somewhere other than your OAuth stack — an unusual access pattern, a provider security notice, a leaked repository.

**Cause:** no rotation, so no reuse detection. A copied refresh token is indistinguishable from the original, and without rotation both work forever, in parallel, silently.

**Fix:** rotate, and wire the detection to a human. Enable rotation on the authorization server, store the newest value atomically, and treat `invalid_grant` on a token you believe is current as a security event rather than a re-consent trigger. This is the failure that most rewards getting right in advance, because the detection has to already exist at the moment of theft — you cannot add it retroactively to a token that was stolen last month.

### The user revoked access and the agent kept working

**Symptom:** a user withdraws consent, the refresh stops working immediately, and the agent nevertheless completes several more downstream calls before anything fails.

**Cause:** two independent lags. [RFC 7009 section 2.1](https://www.rfc-editor.org/rfc/rfc7009#section-2.1) says invalidation "takes place immediately" but acknowledges "there could be a propagation delay, for example, in which some servers know about the invalidation while others do not". And tokens minted by exchange are not covered at all: RFC 8693 section 2.1 says the exchange creates no tight linkage between input and output, so a downstream token issued five minutes ago outlives the revocation of the token it came from.

**Fix:** short lifetimes on exchanged tokens, measured in minutes rather than hours, so the window closes on its own. Revoke the refresh token explicitly at offboarding rather than waiting for expiry. And make revocation a first-class outcome in the agent: when a refresh returns `invalid_grant` mid-run, stop the run rather than finishing the work already in flight.

### An access token is accepted by a service it was never meant for

**Symptom:** a token minted for one internal service works against another, and you discover it by accident.

**Cause:** the token has no audience restriction, or it has one and no resource server checks it. The second case is more common and more embarrassing: the authorization server sets `aud` correctly and every service validates the signature, the expiry, and nothing else.

**Fix:** validation on the receiving side is the control, not issuance. RFC 9068 section 4 spells out the full list a resource server must check on a JWT access token: the `typ` header is `at+jwt` or `application/at+jwt`, `iss` matches exactly, `aud` contains an identifier this server expects for itself, the signature verifies with an algorithm that is not `none`, and the current time is before `exp`. Put that in one shared middleware rather than in each service, and test the negative case — a token minted for a sibling service must be rejected, and that assertion belongs in your test suite.

### The token exchange returns something that is not an access token

**Symptom:** `token_type` comes back as the literal `N_A`, or the value in `access_token` will not authenticate anything.

**Cause:** you set `requested_token_type` to something other than `urn:ietf:params:oauth:token-type:access_token` — an ID token, most often, copied from an example. RFC 8693 section 2.2.1 reuses the `access_token` member to carry whatever was issued, "for historical reasons", and signals what it actually is through `issued_token_type`, using `token_type: N_A` when the result is not usable as an access token.

**Fix:** branch on `issued_token_type`, never on the presence of `access_token`. If you want a bearer token for a downstream API call, request `urn:ietf:params:oauth:token-type:access_token` explicitly rather than omitting `requested_token_type` and accepting the server's default.

## Doing this at scale

One agent, one provider, one flow is an afternoon. The shape that costs real time arrives at the third integration: several providers, each with its own spelling of the same idea, several flows in play at once, and one question nobody can answer from the code — which token belongs to which agent acting for which user, right now.

The concrete load looks like this. Refresh tokens per user per provider, encrypted, rotated, and revoked on offboarding the same day. A consent record per grant, kept current through re-consent. An exchange at every internal service boundary, each with its own audience and its own short lifetime. Per-provider quirks encoded somewhere a human can read: this one takes `audience`, that one takes `resource`, this one rotates refresh tokens and that one does not, this one calls the exchange grant by its RFC name and that one has a proprietary parameter that means almost the same thing. And an audit trail that answers the attribution question without anyone grepping application logs. Every item on that list is lifecycle work, and none of it is the feature you set out to build.

That lifecycle is what [Agentic Fabriq](https://agenticfabriq.com) is a control layer for. Credentials live in the layer rather than in the agent, so the agent holds one gateway token and names a connection instead of holding a provider refresh token. Policy is evaluated per request, and every call is attributed to an agent and the user it acted for — which is the delegation record RFC 8693 describes, produced as a matter of course rather than as a thing each integration remembers to write.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        result = await af.invoke_connection(
            "billing_readonly",
            method="list_invoices",
            parameters={"customer_id": "cus_example", "limit": 5},
        )
        for invoice in result.get("invoices", []):
            print(invoice)


asyncio.run(main())
```

The runnable version is `examples/govern-oauth-flows/delegated_downstream_call.py`. Connection names, method names, and response keys are per-deployment, so run `afctl tools list` against your own gateway rather than trusting `billing_readonly` or `list_invoices` here. The property that matters is not the shorter code — it is that the agent process never holds the user's provider token, so a compromised agent leaks a revocable gateway token instead of a standing grant. Everything in this guide stays correct if you would rather own that lifecycle yourself; a layer like [Agentic Fabriq](https://agenticfabriq.com) is the buy side of the same build-or-buy decision.

## Further reading

The [governance pillar](/govern/) is where the policy side of this lives — deciding which scopes an agent may ever request, rather than implementing the flow that requests them. The [connect pillar](/connect/) has the provider-specific versions, and [connecting an agent to Gmail](/connect/gmail/) is a worked example of the authorization code half of this guide against a real API, including what a restricted-scope consent screen costs you in calendar time. The [failure pillar](/fail/) collects what happens after a token escapes.

Primary sources for everything asserted above:

- [RFC 6749 — The OAuth 2.0 Authorization Framework](https://www.rfc-editor.org/rfc/rfc6749): [section 4.4](https://www.rfc-editor.org/rfc/rfc6749#section-4.4) client credentials, [section 5.1](https://www.rfc-editor.org/rfc/rfc6749#section-5.1) the token response and the `scope` rule, [section 5.2](https://www.rfc-editor.org/rfc/rfc6749#section-5.2) error codes, [section 6](https://www.rfc-editor.org/rfc/rfc6749#section-6) refreshing, [section 10.4](https://www.rfc-editor.org/rfc/rfc6749#section-10.4) refresh token rotation and reuse detection.
- [RFC 7636 — Proof Key for Code Exchange](https://www.rfc-editor.org/rfc/rfc7636): [section 1](https://www.rfc-editor.org/rfc/rfc7636#section-1) the authorization code interception attack, [section 4.1](https://www.rfc-editor.org/rfc/rfc7636#section-4.1) the verifier, [section 4.2](https://www.rfc-editor.org/rfc/rfc7636#section-4.2) the `S256` transformation, [section 4.6](https://www.rfc-editor.org/rfc/rfc7636#section-4.6) server-side verification.
- [RFC 8693 — OAuth 2.0 Token Exchange](https://www.rfc-editor.org/rfc/rfc8693): [section 1.1](https://www.rfc-editor.org/rfc/rfc8693#section-1.1) delegation versus impersonation, [section 2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.1) request parameters, [section 2.2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.2.1) the response, [section 2.2.2](https://www.rfc-editor.org/rfc/rfc8693#section-2.2.2) errors, [section 3](https://www.rfc-editor.org/rfc/rfc8693#section-3) token type identifiers, [section 4.1](https://www.rfc-editor.org/rfc/rfc8693#section-4.1) the `act` claim.
- [RFC 9700 — Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700): [section 2.1.1](https://www.rfc-editor.org/rfc/rfc9700#section-2.1.1) PKCE for all client types, [section 2.1.2](https://www.rfc-editor.org/rfc/rfc9700#section-2.1.2) the implicit grant, [section 2.3](https://www.rfc-editor.org/rfc/rfc9700#section-2.3) privilege restriction, [section 2.4](https://www.rfc-editor.org/rfc/rfc9700#section-2.4) the password grant, [section 4.9.3](https://www.rfc-editor.org/rfc/rfc9700#section-4.9.3) replay countermeasures, [section 4.14.2](https://www.rfc-editor.org/rfc/rfc9700#section-4.14.2) refresh token recommendations.
- [RFC 8414 — Authorization Server Metadata](https://www.rfc-editor.org/rfc/rfc8414#section-3) — the discovery document, the well-known path construction, and metadata validation.
- [RFC 9068 — JWT Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068#section-4) — what a resource server must validate, including `aud`.
- [RFC 8707 — Resource Indicators for OAuth 2.0](https://www.rfc-editor.org/rfc/rfc8707#section-2) — the `resource` parameter and audience-restricting issued tokens.
- [RFC 7009 — Token Revocation](https://www.rfc-editor.org/rfc/rfc7009#section-2.1) — `token`, `token_type_hint`, propagation delay, and cascading revocation.
- [RFC 8252 — OAuth 2.0 for Native Apps](https://www.rfc-editor.org/rfc/rfc8252#section-7.3) — loopback interface redirection for locally run clients.
- [Keycloak: configuring and using token exchange](https://www.keycloak.org/securing-apps/token-exchange) — the provider-specific facts cited above: `audience` support, absent `resource` support, the confidential-client requirement, the `aud` precondition on the subject token, the `downscope-assertion-grant-enforcer` executor, and the experimental status of delegation support.

One note on a document you will be pointed at: OAuth 2.1 consolidates most of the advice above into a single specification, but as of its fifteenth revision in March 2026 it is [an Internet-Draft](https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/), not an RFC. Cite RFC 9700 when you need something a reviewer will accept as current.

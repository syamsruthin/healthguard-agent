# ADR-0020: Short-lived JWT access tokens and rotating refresh-token families

- **Status:** Accepted (`ea1eaeb`, PII removed from claims in `f2c1515`)
- **Area:** Security / API

## Context

The API serves a browser UI, a CLI and direct HTTP clients. It needs stateless authentication on every request (including long SSE streams), the ability to revoke sessions, and protection against stolen refresh tokens. It also holds health data, so tokens must not leak personal information.

## Decision

Built-in auth ([core/security.py](../../backend/core/security.py), [services/auth_service.py](../../backend/services/auth_service.py)):

- **Passwords:** argon2id via `pwdlib`; minimum 10 characters.
- **Access token:** HS256 JWT, **15 minutes**, claims `sub` (user id), `role`, `iss`, `iat`, `exp`, `jti`, `typ=access`. **No email, name or member id.** The server loads the user and member link by `sub` on every request.
- **Refresh token:** an opaque 48-byte random string. Only its **SHA-256 hash** is stored, with a `family_id`, `expires_at` (7 days), `revoked_at` and `replaced_by`.
- **Rotation with reuse detection:** every refresh revokes the presented token and issues a successor in the same family (row locked `FOR UPDATE`). Presenting an **already-revoked** token means it was stolen or replayed, so the **whole family** is revoked.
- **Logout** revokes the family.
- **Timing equalisation:** login with an unknown email still verifies against a dummy argon2 hash, so response time doesn't reveal which emails exist.
- **Ownership:** another user's conversation returns **404, never 403**, so ids can't be probed.
- **Self-registration** creates accounts that are **never linked** to member records; linking would need identity verification.
- **Production guard:** `ENV=production` refuses to start with the default `JWT_SECRET`.

## Why

- **Short-lived access tokens limit the damage of a leak** to 15 minutes, without a database lookup to validate each token.
- **Opaque, hashed refresh tokens** can be revoked, and a database dump doesn't yield usable tokens.
- **Rotation + family revocation** is the standard defence against refresh-token theft (OAuth 2.0 Security BCP): the attacker and the legitimate client can't both keep using the family without one of them presenting a revoked token, which kills both sessions.
- **No PII in tokens.** JWTs are base64, not encrypted; they end up in logs, browser storage and proxies. The engineering review removed email and member id from the claims.
- **404 for others' resources** avoids an existence oracle.
- **Built-in, not an identity provider,** because the demo must run offline in Docker. The design (short JWT + rotating refresh) maps directly onto an OIDC provider later.

## Alternatives considered

| Option | Why not (here) |
|---|---|
| Server-side sessions with cookies | Needs CSRF protection and sticky or shared session storage; bearer tokens suit API + SSE + CLI |
| Long-lived JWTs | Can't be revoked |
| Refresh tokens as JWTs | Can't be revoked without a deny-list anyway; opaque + hashed is simpler |
| An external IdP (Auth0, Cognito, Keycloak) | Correct for production; would make the demo depend on an external service |
| bcrypt | Fine, but argon2id is the current OWASP recommendation (memory-hard) |

## Consequences

- **Positive:** stateless request auth, revocable sessions, theft detection, no PII in tokens.
- **Negative:** HS256 uses a shared secret, so every service that verifies tokens must hold it. With multiple services, switch to RS256/EdDSA with a JWKS endpoint.
- **Tests:** rotation, reuse detection, expiry and rate limits run as integration tests ([tests/integration/api/test_auth_api.py](../../tests/integration/api/test_auth_api.py)).

## Interview soundbite

> "Access tokens are 15-minute JWTs that carry only the user id and role. Refresh tokens are opaque, stored as hashes, and rotate in families: replaying an old one revokes the whole family, which is how you detect a stolen token. Login takes the same time whether the account exists, and other users' resources return 404."

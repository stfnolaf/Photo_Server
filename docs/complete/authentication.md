# V1 authentication and request protection

This session implements one account, not a user-management system. Set
`PHOTO_AUTH_ENABLED=true` to turn it on. Startup then requires:

- `PHOTO_PASSWORD_HASH`, in the local `pbkdf2_sha256$iterations$salt$digest`
  format produced by `photo_server.auth.hash_password`;
- `PHOTO_SESSION_SECRET`, a random value of at least 32 characters; and
- `PHOTO_API_TOKEN`, a separate random value of at least 32 characters.

The password is never stored by the server. The browser receives an HTTP-only,
signed session cookie with the configured SameSite and Secure flags. Sessions
contain only an expiry and random nonce, and expire after
`PHOTO_SESSION_TTL_SECONDS` (12 hours by default). Logout expires the cookie.

`POST /auth/login`, `GET /auth/session`, `POST /auth/logout`, and `GET /livez`
are public. Every other route—including assets, originals, previews, uploads,
mutations, albums, people, `/health`, `/readyz`, `/metrics`, `/docs`, and
`/openapi.json`—requires either the session cookie or
`Authorization: Bearer $PHOTO_API_TOKEN`. Unauthenticated requests receive a
generic 401 before resource lookup, so private asset existence is not exposed.

Cookie-authenticated state-changing requests require either no `Origin` header
(same-origin clients commonly omit it), the public request origin, or an origin
listed in `PHOTO_CORS_ORIGINS`. CORS is only enabled for explicit origins and
credentials are allowed only with that allowlist; wildcard credentialed CORS is
not supported. Put the API behind HTTPS and leave
`PHOTO_SESSION_COOKIE_SECURE=true` in production.

For automation, use `photo-upload --api-token TOKEN` or set
`PHOTO_API_TOKEN`. The CLI sends the token as a bearer header and never prints
it. Treat the token as a deployment secret and rotate it by replacing the
configured value and restarting the API/CLI jobs.

This slice intentionally excludes multi-user roles, OAuth, password reset,
account management, backup replication, restore verification, and storage
cleanup.

## Public-web boundary

This local password flow is appropriate for a trusted home network or a
private VPN, but it is not the intended long-term public-web identity system.
Before exposing the service broadly, prefer OIDC (or an identity-aware reverse
proxy) with TLS, provider-managed MFA/recovery, an allowlist for the single
account, and short-lived API credentials for automation. The API should then
validate the provider-issued identity and keep the same private-route and
CSRF/origin policy; do not replace the browser session with a long-lived OIDC
access token in a cookie. OIDC is deliberately deferred until the deployment,
identity-provider, and account-linking design is ready.

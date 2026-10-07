# Session 3: Authentication and request protection

Work in `/home/stephen/dev/photo_server`.

Implement the first single-user authentication slice. Do not implement backup
replication, restore verification, or storage cleanup in this session.

## Context

The V1 API currently has no authentication. The frontend uses a same-origin
nginx proxy, while the `photo-upload` CLI needs a non-browser authentication
path. The project is intended primarily as a one-user library, not a full
multi-tenant service.

## Recommended design

- HTTP-only, signed session cookie for the browser.
- Separate static API token for the upload CLI and automation.
- Password supplied as a hash/configuration secret, never stored plaintext.
- SameSite cookie and secure-cookie configuration for reverse-proxy HTTPS.
- Strict Origin checking or CSRF token protection for cookie-authenticated
  state-changing requests.

## Requirements

- Add login, logout, and current-session endpoints.
- Add configuration and safe startup validation for the password hash,
  session secret, auth enablement, and API token.
- Protect asset reads, originals, previews, uploads, mutations, albums, people,
  operational health details, and API documentation according to a documented
  policy.
- Leave liveness available without credentials.
- Support the upload CLI through an explicit bearer-token option and environment
  variable without printing the token.
- Reject unsafe cross-origin cookie mutations.
- Do not use wildcard CORS with credentialed requests.
- Redact authentication failures and avoid revealing whether private assets
  exist to unauthenticated callers.
- Add the minimal frontend login/logout/session handling required for the web
  application to function.

## Verification

- Test login success/failure, logout, session expiry, and cookie flags.
- Test browser session access and API-token access independently.
- Test unauthenticated reads and mutations.
- Test CSRF/Origin rejection and CORS behavior.
- Test the upload CLI against an authenticated API.
- Update OpenAPI, generated client code, and contract fixtures as needed.
- Run backend and frontend checks.

## Stop condition

Do not attempt multi-user roles, OAuth, password reset, or account management.
Those require a separate design. Finish with security assumptions and the
deployment configuration documented.

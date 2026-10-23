# Authentication and authorization

## Accounts and access

Public registration (`POST /v1/auth/register`) creates patient accounts only. It requires an `Idempotency-Key`; a repeated key and identical body replays the original response, while changed content with that key returns 409. Email is normalized to lowercase and protected by a database uniqueness constraint. Doctor/admin roles must be provisioned by an operator with `python -m scripts.provision_user`; never permit role selection on public signup.

## Credential and token lifecycle

- Passwords use Argon2id via `pwdlib`; registration and operator provisioning require at least 12 characters.
- Access tokens are HS256 JWTs, 10-minute default TTL, with issuer, issued/expiry timestamps, unique token ID, subject, role, token type, and MFA verification claim. The authorization dependency checks account activity/deletion and verifies the token role still matches the database role.
- Refresh tokens are random 48-byte opaque tokens; only SHA-256 hashes are stored. Refresh rotates the token and carries a family identifier. Reuse of a rotated token revokes all active sessions in that family.
- Every auth mutation requires `Idempotency-Key`. The scoped request fingerprint is stored as a hash; replay responses (including tokens) are AES-GCM encrypted at rest using `DATA_ENCRYPTION_KEY`.
- Keep `JWT_SECRET` and `DATA_ENCRYPTION_KEY` distinct, random, and provisioned from a secret manager. Key rotation needs a versioned re-encryption procedure; retain old decrypt capability until stored auth replay responses and encrypted identity fields are migrated.

## MFA

Authenticated users re-enter their password at `POST /v1/auth/mfa/setup` to create a pending TOTP secret, then call `POST /v1/auth/mfa/enable` with a current code to enable it. Pending and active TOTP secrets are encrypted at rest. When MFA is enabled, login requires a current TOTP code. The access token after successful enrollment carries `mfa=true`; new logins and refreshes preserve the account's MFA state. Sensitive-resource/privileged endpoints must use both role authorization and an MFA claim check. Recovery codes, device management, and account recovery require product/security design before production use.

## Operations and boundaries

The API returns generic credential failures and performs a dummy password verification for unknown users to reduce account enumeration. Authentication rate limits are enforced by middleware and use shared Redis in production; requests fail closed if that backend is unavailable. Configure trusted TLS ingress, prevent tokens from entering logs/URLs, and apply browser token storage policy at the client. JWT role claims are not trusted alone: role and account status are compared with current database state on each request.

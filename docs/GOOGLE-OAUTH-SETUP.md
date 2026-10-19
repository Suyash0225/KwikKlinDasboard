# Google OAuth setup for Kwik Klin

This checklist covers the two separate Google redirect flows used by the Control panel. They can use the same OAuth client ID/secret, but **both callback URLs must be registered exactly** in Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID.

## Production environment

Set these values in the server's secret/environment configuration (do not commit credentials):

```dotenv
SITE_URL=https://kwikklin.online
CONTROL_GOOGLE_BASE_URL=https://kwikklin.online/control
GOOGLE_CLIENT_ID=<OAuth client ID>
GOOGLE_CLIENT_SECRET=<OAuth client secret>
CONTROL_GOOGLE_ALLOWED_EMAILS=<comma-separated trusted Google account emails>
CONTROL_GOOGLE_LEVEL=write
```

Use `CONTROL_GOOGLE_LEVEL=read` for read-only access or `danger` only when the account is intentionally allowed to perform privileged Control-panel operations. Do not leave the allowlist empty in production. Keep the allowed emails tightly scoped to trusted operators.

## Required authorized redirect URIs

Register both of these exact URLs on the same OAuth client:

1. Control-panel sign-in: `https://kwikklin.online/control/auth/google/callback`
2. Google Business Profile connection: `https://kwikklin.online/control/api/google-business/callback`

If the deployed public domain or Control-panel prefix changes, update both Google Cloud Console entries and the matching environment variables. Google requires an exact redirect-URI match; a trailing slash or different path can cause `redirect_uri_mismatch`.

For local development, use the exact callback URL printed by the app's generated Google authorization URL rather than copying the production URLs. Do not add localhost URLs to the production OAuth client unless they are intentionally needed.

## Verification checklist

- [ ] `SITE_URL` is the canonical HTTPS public domain.
- [ ] `CONTROL_GOOGLE_BASE_URL` is the public Control-panel base URL, including `/control`.
- [ ] `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are present in the production secret store and belong to the same OAuth client.
- [ ] `CONTROL_GOOGLE_ALLOWED_EMAILS` contains only the intended operator accounts.
- [ ] Both redirect URIs above are registered in Google Cloud Console.
- [ ] Control-panel login completes and rejects a Google account not on the allowlist.
- [ ] From Control → tenant → Google Business Profile, the intended Google account can connect and the expected business locations appear.
- [ ] The GBP status page shows the connected state without exposing tokens.
- [ ] Test connection and posting with a non-production/test profile first. Do not trigger a live post merely to validate OAuth configuration.

## Important boundaries

A green CI run proves code-level tests only. It does not prove that production environment variables, Google Cloud Console redirect URIs, Google account permissions, or live GBP posting are configured correctly. Verify those separately during an authorized rollout.

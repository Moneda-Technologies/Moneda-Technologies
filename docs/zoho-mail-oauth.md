# Zoho Mail OAuth setup

Moneda sends transactional email only through the Zoho Mail REST API. SMTP credentials and SMTP fallback are not used.

## Zoho API Console

Create or edit the application at the India Zoho API Console with:

- Client type: **Server-based Application**
- Data center: **India**
- Local authorized redirect URI: `http://localhost:5005/api/v1/integrations/zoho/callback`
- OAuth scopes: `ZohoMail.messages.CREATE,ZohoMail.accounts.READ`

`ZohoMail.messages.CREATE` is the minimum send/upload permission. Moneda also needs the read-only `ZohoMail.accounts.READ` permission because Zoho's `/api/accounts` endpoint is the documented way to discover and validate the authenticated mailbox account ID. `ZohoMail.messages.ALL` is not requested.

The redirect URI must match exactly, including scheme, host, port, path, and the use of `localhost` rather than `127.0.0.1`.

## Local environment

Configure these server-only values in the repository `.env`:

```dotenv
EMAIL_PROVIDER=zoho_mail_api
ZOHO_CLIENT_ID=your_server_application_client_id
ZOHO_CLIENT_SECRET=your_server_application_client_secret
ZOHO_REFRESH_TOKEN=
ZOHO_ACCOUNT_ID=
ZOHO_ACCOUNTS_BASE_URL=https://accounts.zoho.in
ZOHO_OAUTH_REDIRECT_URI=http://localhost:5005/api/v1/integrations/zoho/callback
ZOHO_MAIL_API_BASE_URL=
ZOHO_FROM_ADDRESS=business@monedatechnologies.com
INTEGRATION_ENCRYPTION_KEY=a_stable_high_entropy_deployment_secret
MAIL_TEST_TO=
```

Do not place real values in `.env.example`. `INTEGRATION_ENCRYPTION_KEY` must remain stable after connecting; changing it makes an existing encrypted refresh token unreadable. For compatibility, the backend derives token encryption from Flask `SECRET_KEY` when the dedicated key is omitted.

`ZOHO_MAIL_API_BASE_URL` can remain blank. The backend stores Zoho's returned OAuth `api_domain`, maps its regional suffix to the Mail API domain, and uses `https://mail.zoho.in` for the India data center. An explicit environment value is only a fallback.

After the authorization code exchange succeeds, the backend discovers the Mail account ID by calling `GET https://mail.zoho.in/api/accounts` with `Authorization: Zoho-oauthtoken <access_token>`. Zoho's OAuth token response uses `token_type: Bearer`, but the Zoho Mail REST API documentation requires the `Zoho-oauthtoken` header prefix for Mail API requests. The account lookup reads `accountId` from the selected row in the response `data` array and matches `business@monedatechnologies.com` against documented mailbox fields such as `primaryEmailAddress`, `mailboxAddress`, `incomingUserName`, and `emailAddress[].mailId`. It must not use Zoho user ID (`zuid`) or organization ID (`zoid`) as the Mail account ID.

## Connect and verify

1. Start Flask from `backend` with `python run.py`.
2. Start the frontend on `http://localhost:3005`.
3. Sign in as an Admin or Superadmin.
4. Open **Settings -> Communication -> Zoho Mail**.
5. Select **Connect Zoho Mail**, authorize `business@monedatechnologies.com`, and return through the callback.
6. Confirm that OAuth, account ID, and API domain show connected/configured.
7. Enter a recipient and select **Test Email**. Confirm the single message arrives.
8. Exercise signup OTP, login OTP, quotation send, order confirmation, and order status email.

The backend generates a fresh state for every authorization URL and includes it in the Zoho request, but does not assume Zoho will echo that optional parameter in the server-based callback. Each attempt also uses Zoho's server-based PKCE extension: the S256 verifier is encrypted in MongoDB and only the challenge is sent to Zoho. MongoDB stores a one-time, user-bound OAuth transaction; the signed Flask session holds only its opaque transaction pointer. A returned state must match the transaction, while a callback without state is accepted only through that unexpired, one-time server transaction and the PKCE verifier. OAuth codes and tokens never appear in the frontend response or logs. Disconnect attempts remote token revocation and always removes the local encrypted credential.

## Production migration

No application code changes are required. Set:

```dotenv
ZOHO_OAUTH_REDIRECT_URI=https://YOUR-PRODUCTION-DOMAIN/api/v1/integrations/zoho/callback
APP_BASE_URL=https://YOUR-PRODUCTION-DOMAIN
FRONTEND_ORIGIN=https://YOUR-PRODUCTION-DOMAIN
SESSION_COOKIE_SECURE=true
```

Register that exact HTTPS callback in the same Zoho server-based application before connecting from production. Keep the India accounts base unless the mailbox moves to another Zoho data center.

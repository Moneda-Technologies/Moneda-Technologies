# Zoho WorkDrive profile assets

Moneda keeps profile photos and email signatures on the backend's local upload volume and optionally synchronizes a private copy to Zoho WorkDrive. Local storage remains the runtime source of truth, so profile uploads and private file serving continue to work during a Zoho outage or when WorkDrive is disabled.

## OAuth scopes

Create a server-side Zoho OAuth refresh token with these least-privilege scopes:

- `WorkDrive.files.CREATE`
- `WorkDrive.files.READ`
- `WorkDrive.files.UPDATE`
- `WorkDrive.files.DELETE` (required only for mirroring user removals)

The Zoho identity must have editor or higher access to the configured root folder. Files are never published or shared publicly.

## Environment variables

```text
ZOHO_WORKDRIVE_ENABLED=true
ZOHO_WORKDRIVE_CLIENT_ID=
ZOHO_WORKDRIVE_CLIENT_SECRET=
ZOHO_WORKDRIVE_OAUTH_REDIRECT_URI=http://localhost:5005/api/v1/integrations
ZOHO_WORKDRIVE_REFRESH_TOKEN=
ZOHO_WORKDRIVE_ROOT_FOLDER_ID=
ZOHO_WORKDRIVE_ACCOUNT_REGION=in
ZOHO_WORKDRIVE_API_BASE_URL=https://www.zohoapis.in/workdrive/api/v1
ZOHO_WORKDRIVE_TIMEOUT_SECONDS=8
```

`ZOHO_WORKDRIVE_CLIENT_ID` and `ZOHO_WORKDRIVE_CLIENT_SECRET` take precedence when set; otherwise WorkDrive reuses `ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET`. The grants remain independent even when they share one OAuth client. `ZOHO_WORKDRIVE_REFRESH_TOKEN` is an optional deployment fallback: the normal browser OAuth flow stores the WorkDrive refresh token encrypted in the `zoho_workdrive` MongoDB integration record.

`ZOHO_WORKDRIVE_ROOT_FOLDER_ID` should identify the private Moneda `Users` folder. The integration creates `<user_id>/Profile` below it. Use the matching Zoho data-center region (`us`, `eu`, `in`, `au`, `jp`, `ca`, `ae`, or `sa`). Credentials stay server-side and are never included in profile API responses or user documents.

The same Zoho server-based application may be used for Mail and WorkDrive, but its authorized redirect URI list must contain both exact entries:

- `http://localhost:5005/api/v1/integrations` for WorkDrive
- `http://localhost:5005/api/v1/integrations/zoho/callback` for Mail

Use **Settings -> Zoho WorkDrive -> Connect Zoho WorkDrive** to authorize. The backend uses offline consent, PKCE, and a one-time `zoho_workdrive` state, then stores the resulting refresh token encrypted in MongoDB. Zoho API Console redirect registration is a manual external step.

## Synchronization behavior

1. The backend validates actual PNG, JPEG, or WEBP content, extension, size (maximum 2 MB), and dimensions.
2. It atomically replaces the user's local file under `uploads/profile-photos` or `uploads/signatures`.
3. It updates local MongoDB metadata.
4. When enabled, it finds or creates the stable `<user_id>/Profile` folder and uploads `photo.<ext>` or `signature.<ext>` with name override enabled. Repeated synchronization creates a new file version rather than duplicate names.
5. A successful copy is marked `SYNCED`. A safe error code is stored as `FAILED`; the local upload remains available and can be retried.

With `ZOHO_WORKDRIVE_ENABLED=false`, no WorkDrive HTTP request is made and the previous local-only behavior is preserved.

## Administrative retry

Administrative synchronization endpoints are permission protected:

```text
POST /api/v1/admin/workdrive/resync-user/<user_id>
POST /api/v1/admin/workdrive/resync-all
POST /api/v1/admin/workdrive/resync-failed
POST /api/v1/admin/workdrive/test
GET  /api/v1/admin/workdrive/status
```

The bulk operation skips users without local assets, never deletes local files, and reports synchronized, failed, and skipped counts.

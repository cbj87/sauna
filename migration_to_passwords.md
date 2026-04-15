# Migration to Email/Password Login — Step-by-Step Checklist

Replace the PIN-based login (which exposes all member names) with email + password.
Existing users migrate by verifying their PIN once, then setting email + password.
New users sign up with email + password (pending admin approval).
Self-service password reset via emailed token link.

---

## Step 1 — Update `models.py`: Add new columns

- [x] Add `email` column to `FamilyMember` (`String, nullable=True, unique=True`)
- [x] Add `password_hash` column to `FamilyMember` (`String, nullable=True`)
- [x] Add `reset_token` column to `FamilyMember` (`String, nullable=True`)
- [x] Add `reset_token_expires` column to `FamilyMember` (`DateTime, nullable=True`)
- [x] Keep `pin_hash` column as-is (still used during migration flow, then cleared)
- [x] Update `to_dict()` to include `email` field
- [x] Update `to_public_dict()` to include `has_credentials` bool (`email is not None`)

---

## Step 2 — Update `models.py`: DB migrations

- [x] Add migration: `ALTER TABLE family_members ADD COLUMN email TEXT`
- [x] Add migration: `ALTER TABLE family_members ADD COLUMN password_hash TEXT`
- [x] Add migration: `ALTER TABLE family_members ADD COLUMN reset_token TEXT`
- [x] Add migration: `ALTER TABLE family_members ADD COLUMN reset_token_expires DATETIME`
- [x] Add partial unique index: `CREATE UNIQUE INDEX IF NOT EXISTS ix_fm_email ON family_members (email) WHERE email IS NOT NULL`

---

## Step 3 — Update `harvia_server.py`: SMTP + helpers

- [x] Add SMTP environment variables at startup:
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `APP_URL`
- [x] Add `_send_email(to, subject, body_text)` helper using stdlib `smtplib` + `email.mime`
  - No-ops silently (logs warning) if SMTP not configured
- [x] Add `_validate_password(pw)` helper (returns error string or `None`; min 8 chars)

---

## Step 4 — Update `POST /api/auth/signup`

- [x] Accept `{name, email, password, color}` instead of `{name, pin, color}`
- [x] Validate name, email format, password (≥8 chars)
- [x] Check email uniqueness (return 409 if taken)
- [x] Hash password with bcrypt; store as `password_hash`
- [x] Leave `pin_hash = None` for new accounts
- [x] First user → admin + approved; others → pending (same logic as before)

---

## Step 5 — Update `POST /api/auth/login`

- [x] Accept `{email, password}` instead of `{member_id, pin}`
- [x] Look up member by email (case-insensitive)
- [x] If not found: record failed attempt, return generic 401 ("Invalid email or password")
- [x] Status checks (pending/rejected) remain the same
- [x] Verify password: `bcrypt.checkpw(password.encode(), member.password_hash.encode())`
- [x] Rate limiting (10 attempts / 15 min per IP) remains unchanged

---

## Step 6 — Add `POST /api/auth/migrate`

- [x] Create new endpoint (CSRF exempt — pre-auth)
- [x] Accept `{member_id, pin, email, password}`
- [x] Look up member by `member_id`
- [x] Verify `pin_hash` exists and PIN matches (bcrypt check)
- [x] If member already has email: return 409 "Account already migrated — please log in with your email"
- [x] Validate email format + uniqueness
- [x] Validate password (≥8 chars)
- [x] Update member: set `email`, set `password_hash`, clear `pin_hash = None`
- [x] Set session (log them in immediately)
- [x] Return `{ok: true, member: {...}, csrf_token: "..."}`
- [x] Add `"migrate"` to `_CSRF_EXEMPT` list

---

## Step 7 — Add `POST /api/auth/forgot-password`

- [x] Create new endpoint (CSRF exempt — pre-auth)
- [x] Accept `{email}`
- [x] Always return HTTP 200 (don't reveal whether email exists)
- [x] Look up member by email; if not found, silently return
- [x] Generate token: `secrets.token_urlsafe(32)`
- [x] Store on member: `reset_token = token`, `reset_token_expires = now + 1 hour`
- [x] Send email with link: `{APP_URL}/?reset_token={token}`
- [x] Add `"forgot_password"` to `_CSRF_EXEMPT` list

---

## Step 8 — Add `POST /api/auth/reset-password`

- [x] Create new endpoint (CSRF exempt — pre-auth)
- [x] Accept `{token, new_password}`
- [x] Validate `new_password` (≥8 chars)
- [x] Find member where `reset_token == token`
- [x] If not found or expired (`reset_token_expires < now`): return 400 "Reset link is invalid or has expired"
- [x] Update member: set `password_hash`, clear `reset_token`, clear `reset_token_expires`
- [x] Set session (log them in immediately)
- [x] Return `{ok: true, member: {...}, csrf_token: "..."}`
- [x] Add `"reset_password"` to `_CSRF_EXEMPT` list

---

## Step 8b — Add admin escape hatch: `PUT /api/admin/members/<id>/set-credentials`

For existing users who have forgotten their PIN and cannot use the self-service migration flow.

- [x] Create new admin-only endpoint `PUT /api/admin/members/<id>/set-credentials`
- [x] Require active admin session (same auth guard as other `/api/admin/` routes)
- [x] Accept `{email, password}`
- [x] Validate email format + uniqueness (return 409 if taken)
- [x] Validate password (≥8 chars)
- [x] Update member: set `email`, set `password_hash`, clear `pin_hash = None`
- [x] Return updated member dict
- [x] Add button in AdminPanel member list: "Set credentials" (visible only for members where `has_credentials === false`)
  - Opens a small inline form: email input + password input + confirm password
  - Submits to the new endpoint
  - On success: refresh member list (member disappears from "not yet migrated" view)

---

## Step 9 — Frontend: Auth state machine + URL handling (`static/index.html`)

- [x] Add new auth states: `"migrate"`, `"forgot"`, `"reset"`
- [x] On page load, check `new URLSearchParams(location.search).get('reset_token')`
  - If present: store token in state, go directly to `"reset"` state
- [x] After successful reset: call `history.replaceState({}, '', '/')` to remove token from URL

---

## Step 10 — Frontend: Rebuild `LoginScreen`

- [x] Replace member-picker + PIN pad with:
  - Email input (`<input type="email">`)
  - Password input (`<input type="password">`)
  - "Sign in" button → POST `/api/auth/login`
  - "Forgot password?" link → `"forgot"` state
  - "Migrate existing account" link → `"migrate"` state
  - "Create account" link → `"signup"` state
- [x] Keep session-expired amber banner

---

## Step 11 — Frontend: Update `SignupScreen`

- [x] Replace 3-step PIN flow with 2 steps:
  - Step "info": name + color (unchanged)
  - Step "credentials": email + password + confirm password
    - Client-side validation: passwords match, password ≥8 chars
- [x] POST `/api/auth/signup` with `{name, email, password, color}`
- [x] Response handling identical to before (approved → main, pending → pending)

---

## Step 12 — Frontend: Add `MigrateScreen` component

- [x] 3-step flow:
  1. **Step "pick"**: Fetch `GET /api/members`, show only members where `has_credentials === false`
     - If all members already migrated: show message + back button
  2. **Step "pin"**: Show selected member name + PinPad; on completion, advance to step "credentials"
  3. **Step "credentials"**: Email + password + confirm password; POST `/api/auth/migrate`
     - On success: log in (same as normal login response)
     - On PIN error: back to step "pin" with error message
- [x] Back button at each step; link back to LoginScreen

---

## Step 13 — Frontend: Add `ForgotPasswordScreen` component

- [x] Email input
- [x] Submit → POST `/api/auth/forgot-password`
- [x] On success (always): show "If that email is registered, a reset link has been sent. Check your inbox."
- [x] Back to login link

---

## Step 14 — Frontend: Add `ResetPasswordScreen` component

- [x] Receives `token` prop (from URL param)
- [x] New password + confirm password inputs
- [x] Submit → POST `/api/auth/reset-password` with `{token, new_password}`
- [x] On success: show "Password updated — you're now signed in" + transition to `"main"`
- [x] On error: show error + "Request a new link" button → transition to `"forgot"`

---

## Step 15 — Update `.env.example`

- [x] Add SMTP variables:
  ```
  SMTP_HOST=smtp.sendgrid.net
  SMTP_PORT=587
  SMTP_USER=apikey
  SMTP_PASSWORD=<your-sendgrid-api-key>
  SMTP_FROM=noreply@yourdomain.com
  APP_URL=https://your-app.up.railway.app
  ```

---

## Step 16 — End-to-end verification

- [x] **New signup**: Sign up with email/password → pending → admin approves → log in successfully
- [x] **Migration**: Migrate flow → pick member → enter PIN → set email/password → logged in; verify `pin_hash` cleared in DB browser
- [x] **Login**: Migrated user logs in with email/password; wrong password returns generic error; 10 failed attempts triggers rate limit
- [x] **Forgot password**: Submit email → check server logs for email send; visit reset link → set new password → logged in
- [x] **Bad reset token**: Craft expired/invalid token URL → confirm 400 error shown
- [x] **Non-migrated member**: Confirm they cannot log in via normal login (no email set) and must use migration flow
- [x] **Already migrated**: Confirm migrated member does not appear in migration member-picker
- [x] **Deploy to Railway**: Set SMTP env vars; verify password reset emails arrive

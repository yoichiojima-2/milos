---
name: gcloud-login
description: Log a remote (cloud) Claude Code session in to Google Cloud so gcloud works. Use this whenever a cloud session needs gcloud, Google Cloud credentials, an IAP identity token, the Firestore emulator, Cloud Run or Terraform against GCP, or to test milos against a real deployment; also when the user says "log in to gcloud", "give me the link and I'll paste the code", or asks to run anything that needs `gcloud auth`. Do not assume gcloud is absent or that login is impossible until you have run this skill.
---

# gcloud login in a cloud session

Remote Claude Code containers start with no `gcloud` binary, no stored Google
credentials, and a placeholder `CLOUDSDK_AUTH_ACCESS_TOKEN=proxy-inject` in the
environment. The placeholder makes every gcloud call fail with an invalid token,
so every gcloud command in a cloud session runs as
`env -u CLOUDSDK_AUTH_ACCESS_TOKEN gcloud …`. The scripts below do that for you.

Login is Google's interactive OAuth flow, so the user must open a link on their
own machine and paste back a one-time code. The login process therefore has to
stay waiting across two of your turns; `login.sh` keeps it alive on a named pipe.

## Procedure

Scripts live next to this file in `scripts/`; `SDK` below is the install prefix
(the session scratchpad by default).

1. `scripts/install.sh` — downloads and unpacks the SDK into `<scratchpad>/google-cloud-sdk`
   (skips if present) and prints the `bin` directory. Takes a minute the first time.
2. `scripts/login.sh` — starts `gcloud auth login --no-launch-browser` in the background
   and prints the sign-in URL. Give that URL to the user verbatim and ask for the
   verification code the page shows. End your turn; nothing else can proceed.
3. `scripts/code.sh <code>` — feeds the pasted code to the waiting login and prints the
   result line (`You are now logged in as …`).
4. Confirm with `env -u CLOUDSDK_AUTH_ACCESS_TOKEN "$SDK/bin/gcloud" auth list`.

For the rest of the session, run gcloud through the wrapper the install script prints,
or put `"$SDK/bin"` first on `PATH` and `unset CLOUDSDK_AUTH_ACCESS_TOKEN` in the same
shell command. The milos CLI then mints IAP identity tokens by itself; no
`MILOS_ID_TOKEN` needed.

## Useful follow-ups

- **Firestore emulator** (needs Java, present in the container):
  `gcloud components install cloud-firestore-emulator beta -q`, then follow the
  local-development section of `docs/operations.md` to run the real API and CLI
  against it. This is the most complete test available without a deployment.
- **Terraform**: not installed either; a static binary from releases.hashicorp.com
  unpacks the same way into the scratchpad and lets you run the CI checks in CLAUDE.md.
- **Before assuming a deployment exists**, run `gcloud projects describe <project>`.
  A project in `DELETE_REQUESTED` has nothing running and its URLs are gone.

## Boundaries

- Never print, echo, or "just show the first characters of" an access or identity
  token. The permission classifier blocks it and it proves nothing; prove the login by
  running a command that needs it.
- Stay inside the project the repo names. Do not list or probe the user's other
  projects to find something; ask instead.
- Credentials live in the ephemeral container. Offer `gcloud auth revoke` when the
  work is done, and never copy them anywhere else.

# milos console frontend

Next.js 15 (static export) + React 19 + TypeScript + Tailwind 4, with
shadcn-style components and next-themes (light/dark).

Pages: `/` (sessions you started, sessions you approve, new-session form),
`/session?sid=...` (journal as a chat, approval cards, composer for the
operator), `/approvals` (every call parked on you), `/agents` + `/agent?id=...`
(what is published, read-only). Every request is a `/v1` call on the page's own
origin: the API serves the bundle beside its routes, so the IAP cookie that
admitted the browser is the identity for every action and nothing is
authorised client-side. `src/lib/types.ts` mirrors `src/milos/models.py`.

- `npm run dev` — dev server; proxies `/v1` to `http://127.0.0.1:8080`, so run
  `MILOS_DEV_USER=you@example.com uv run milos serve api` alongside.
- `npm run build` — static export copied into `../src/milos/console/static/`.

The build output is gitignored. The Docker image builds the frontend in its
own Node stage; for a local `milos serve api` with the console, run
`make console` from the repository root (and again after frontend changes).

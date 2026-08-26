# Statement-of-applicability notes — what milos does NOT claim

The honest half of [controls.md](controls.md). An auditor will ask; these are
the answers, decided rather than forgotten.

- **No cryptographic signing of evidence.** Bundles are integrity-protected
  by sha256 manifests plus the bucket's (lockable) retention policy —
  tamper-*evident* against modification in place, and tamper-*proof* only as
  far as the retention lock reaches. KMS asymmetric signing of manifests is
  named future work, not a shipped control.
- **No continuous journal mirroring.** Firestore is the working copy; records
  there are protected against overwrite (doc id == uuid) and against the
  sandbox (no delete grants) but a project owner can delete documents between
  exports. The documented procedure — monthly `milos evidence export` — bounds
  that exposure to the export cadence. Deployments needing a tighter bound
  should export more often.
- **Access Transparency / Access Approval** (visibility into Google-personnel
  access) are org-level GCP features gated on support tier; milos neither
  configures nor claims them.
- **The sandbox agent is the runner service account.** The agent has a shell,
  so IAM — not tool-level checks — is the boundary; tool checks are
  ergonomics. What IAM denies (the evidence bucket, all secrets by default)
  is the guarantee; anything the runner identity can do, a prompt-injected
  agent can do.
- **FQDN egress rules are DNS-based.** With `egress_control = true`, hosts
  sharing an allowed domain's IPs (same CDN edge) are not blocked; SNI-level
  enforcement needs Secure Web Proxy (always-on cost) in front.
- **One project = one trust boundary.** No per-team policies, no
  session-to-session isolation inside the state bucket. Tenant separation is
  project separation.
- **Certification is the operator's.** milos supplies technical controls and
  evidence; the ISO 27001 ISMS / ISO 42001 AIMS (scope, risk treatment,
  internal audit, management review, competence records) is the operating
  organization's management system.

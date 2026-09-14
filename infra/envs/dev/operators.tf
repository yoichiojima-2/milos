# CLI access through IAP. The Google-managed OAuth client admits the users
# group in a browser but rejects programmatic user tokens, so the CLI acts as a
# service account that signs its own JWT (aud = the public service URL). Three
# identities: an approver must not be the session's operator, and publishing
# is an admin action. operator and approver must be members of the users
# group (definitions authorise groups only); admin must be a member of the
# admin group. CI signs for admin to publish definitions.
resource "google_service_account" "operators" {
  for_each     = toset(["operator", "approver", "admin"])
  project      = module.foundation.project_ids.runtime
  account_id   = "milos-${each.key}"
  display_name = "milos ${each.key} (IAP, CLI)"

  depends_on = [module.foundation]
}

resource "google_service_account_iam_member" "operator_signers" {
  for_each = google_service_account.operators

  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "group:${var.users_group}"
}

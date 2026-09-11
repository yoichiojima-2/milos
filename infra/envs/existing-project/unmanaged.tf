# Resources this root no longer manages. They stay in the project untouched;
# the next apply only drops them from the state. Delete this file after that apply.
removed {
  from = google_storage_bucket.state
  lifecycle { destroy = false }
}

removed {
  from = google_secret_manager_secret.anthropic_api_key
  lifecycle { destroy = false }
}

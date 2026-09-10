output "connector_urls" {
  description = "Connector name -> URL, for the runtime module."
  value = {
    egress    = "https://${var.name}-connector-${var.project_number}.${var.region}.run.app"
    web_fetch = "https://${var.name}-web-fetch-${var.project_number}.${var.region}.run.app"
  }
}

output "service_accounts" {
  value = [google_service_account.connector.email, google_service_account.web_fetch.email]
}

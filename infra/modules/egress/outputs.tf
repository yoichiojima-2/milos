output "connector_urls" {
  description = "Connector name -> URL, for the runtime module."
  value = {
    egress    = "https://${var.name}-connector-${var.project_number}.${var.region}.run.app"
    web_fetch = "https://${var.name}-web-fetch-${var.project_number}.${var.region}.run.app"
  }
}

output "service_accounts" {
  description = "Service name -> identity, for the runtime module's extra_internal_invokers."
  value = {
    connector = google_service_account.connector.email
    web_fetch = google_service_account.web_fetch.email
  }
}

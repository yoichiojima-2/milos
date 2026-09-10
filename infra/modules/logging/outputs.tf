output "bucket" {
  value = google_logging_project_bucket_config.audit.id
}

output "sink_writer_identity" {
  value = google_logging_folder_sink.audit.writer_identity
}

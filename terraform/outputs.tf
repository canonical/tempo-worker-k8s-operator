output "app_name" {
  value = juju_application.tempo_worker.name
}

output "requires" {
  value = {
    tempo_cluster = "tempo-cluster"
  }
}
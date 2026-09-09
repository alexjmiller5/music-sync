# Durable retry timestamps

The reconciler shall assign a UTC millisecond `updated_at` to every hub
operation row missing that key before saving pending intent or mutating Spotify
or the hub. This applies to normal reconciliation, observation imports,
tombstones and provenance.

When replaying pending operations, the reconciler shall preserve supplied
timestamps, including explicit null for central rejection. Unstamped pending
rows shall receive a timestamp that is checkpointed before submission. Retries
and restarts shall reuse the persisted timestamp so an old edit cannot overtake
a newer hub edit. Failed initial checkpoints shall prevent remote mutations.

The reconciler shall preserve caller actions and pending payloads. Dry runs
shall neither stamp nor save nor write. Date normalization and the existing
missing-timestamp fallback for direct capture and migration calls remain in
`Hub.push`.

Scope is limited to operation preparation, regression tests and documentation.
No dependencies, production calls, activation changes or deployment are needed.

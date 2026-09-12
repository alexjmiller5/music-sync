# Observed metadata cutover

This is an operational procedure requiring separate approval, not a statement
about the current deployment or catalog. Do not activate cron or start blanket
provider backfills. Keep `RECONCILE_ENABLED=0` throughout.

1. Back up the current catalog, songs, provenance, playlists and memberships
   through supported Life Data catalog/row interfaces. Retain the original
   raw Spotify files through `/v1/files/` and preserve the project recovery
   object through its existing R2 interface. Verify backups are readable and
   record their identities outside source control.
2. Obtain approval for the deployment and capture downtime window. Stop new
   capture, reconciliation, replay and enrichment writers; drain in-flight
   operations. Finish any pending recovery under its original intent before
   unbinding. If it cannot finish, pause the cutover; never clear pending intent
   manually. Keep capture unavailable until verification completes.
3. Read the catalog. Preserve all existing explicit `provenance.from_kind`
   options and append the already-used `http:spotify_isrc` option using the
   supported catalog option interface before removing its last dynamic binding.
   Verify old provenance still validates. Do not relabel or delete old evidence.
   Preserve all property types, constraints and identities.
4. Use the installed Life Data CLI's supported atomic SQL update:

   ```sh
   life sql "UPDATE catalog_properties SET derived_by=NULL, inputs=NULL WHERE tbl='songs' AND col IN ('title','artists','album','album_year','duration_ms','spotify_ids','spotify_playable') AND deleted_at IS NULL"
   ```

   This command shape was verified by the controller in an isolated CLI fixture;
   it has not been run against the live catalog. `property set --inputs ''` is
   not equivalent: it parses a one-element empty-string array. Read back all
   seven live property rows via `GET /v1/catalog` and verify both bindings null.
5. Deploy the reviewed, tested code through the approved CI/CD flow and verify
   its result. Include the separately reviewed non-erasing enrichment service
   change before permitting enrichment again. The existing Life Data checked
   commit guard rejects in-flight writes whose catalog binding changed; this
   does not replace draining writers. Refresh catalog documentation with the
   supported `life doc` workflow. Verify the deployed ownership gate against
   the read-back catalog without enabling enforcement.
6. Use the existing authenticated reconcile endpoint with a retained archive
   key, its actual observation time and `dry_run:true`:

   ```json
   {"metadata_replay":{"archive_key":"raw/spotify-pull/example.json.gz","observed_at":"2026-01-01T00:00:00.000Z"},"dry_run":true}
   ```

   Substitute verified source facts; never invent the archive time or market.
   Review recovered/conflicting/missing-source outcomes and planned field patches.
   Obtain explicit replay approval, then send that same request with boolean
   `dry_run:false`. Resume partial recovery with the identical source identity.
7. Read back songs and provenance. Confirm preserved likes, memberships and
   capture history, coherent album/year pairs, and archive evidence timestamps
   in `detail.observed_at`. Compare against backups; preview again for remaining
   gaps. Restore capture only after its verification gate. Leave cron disabled;
   enforcement activation and any enrichment run need separate authorization.

No new rollback protocol is implied: keep backup and pending evidence intact,
stop affected writers on failure, and review recovery before any restoration.

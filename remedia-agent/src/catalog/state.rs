//! Estado local en SQLite: hashes conocidos por `external_id`, cola de envíos
//! pendientes hacia Remedia y metadatos (último sync, último heartbeat, etc.).

use anyhow::Context;
use rusqlite::{params, Connection, OptionalExtension};
use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;

pub const META_LAST_SYNC_OK: &str = "last_sync_ok_at";
pub const META_LAST_FULL_MANIFEST: &str = "last_full_manifest_at";
pub const META_LAST_HEARTBEAT: &str = "last_heartbeat_at";
pub const META_ERP_STATUS: &str = "erp_status";
pub const META_ERP_VERSION: &str = "erp_version";
pub const META_CATALOG_COUNT: &str = "catalog_count";
pub const META_LAST_SYNC_ERROR: &str = "last_sync_error";
pub const META_LAST_ID_SCAN: &str = "last_id_scan_at";
pub const META_LAST_SYNC_AT: &str = "last_sync_at";
pub const META_LAST_SYNC_FETCHED: &str = "last_sync_fetched";
pub const META_LAST_SYNC_CHANGED: &str = "last_sync_changed";
pub const META_LAST_HEARTBEAT_ERROR: &str = "last_heartbeat_error";
pub const META_METRICS_JSON: &str = "metrics_json";

pub const KIND_CATALOG: &str = "catalog";
pub const KIND_MANIFEST: &str = "manifest";

#[derive(Debug, Clone, PartialEq)]
pub struct Pending {
    pub id: i64,
    pub kind: String,
    pub payload: String,
    pub attempts: u32,
    pub next_try_at: i64,
}

pub struct State {
    conn: Mutex<Connection>,
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS items(
    external_id TEXT PRIMARY KEY,
    hash        TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
";

pub fn now_unix() -> i64 {
    chrono::Utc::now().timestamp()
}

impl State {
    pub fn open(path: &Path) -> anyhow::Result<State> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creando {}", parent.display()))?;
        }
        let conn = Connection::open(path)
            .with_context(|| format!("abriendo {}", path.display()))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        Self::init(conn)
    }

    pub fn open_in_memory() -> anyhow::Result<State> {
        Self::init(Connection::open_in_memory()?)
    }

    fn init(conn: Connection) -> anyhow::Result<State> {
        conn.execute_batch(SCHEMA).context("creando esquema de state.sqlite")?;
        Ok(State { conn: Mutex::new(conn) })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|e| e.into_inner())
    }

    // ---- items -----------------------------------------------------------

    pub fn known_hashes(&self) -> anyhow::Result<HashMap<String, String>> {
        let conn = self.lock();
        let mut stmt = conn.prepare("SELECT external_id, hash FROM items")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
        let mut out = HashMap::new();
        for row in rows {
            let (id, hash) = row?;
            out.insert(id, hash);
        }
        Ok(out)
    }

    pub fn upsert_hashes(&self, items: &[(String, String)]) -> anyhow::Result<()> {
        if items.is_empty() {
            return Ok(());
        }
        let mut conn = self.lock();
        let tx = conn.transaction()?;
        {
            let mut stmt = tx.prepare_cached(
                "INSERT INTO items(external_id, hash, updated_at) VALUES(?1, ?2, ?3)
                 ON CONFLICT(external_id) DO UPDATE SET hash = excluded.hash, updated_at = excluded.updated_at",
            )?;
            let now = now_unix();
            for (id, hash) in items {
                stmt.execute(params![id, hash, now])?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn remove_items(&self, ids: &[String]) -> anyhow::Result<()> {
        if ids.is_empty() {
            return Ok(());
        }
        let mut conn = self.lock();
        let tx = conn.transaction()?;
        {
            let mut stmt = tx.prepare_cached("DELETE FROM items WHERE external_id = ?1")?;
            for id in ids {
                stmt.execute(params![id])?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn count_items(&self) -> anyhow::Result<i64> {
        Ok(self.lock().query_row("SELECT COUNT(*) FROM items", [], |r| r.get(0))?)
    }

    // ---- pending ---------------------------------------------------------

    pub fn enqueue(&self, kind: &str, payload: &str) -> anyhow::Result<i64> {
        let conn = self.lock();
        let now = now_unix();
        conn.execute(
            "INSERT INTO pending(kind, payload, attempts, next_try_at, created_at) VALUES(?1, ?2, 0, ?3, ?3)",
            params![kind, payload, now],
        )?;
        Ok(conn.last_insert_rowid())
    }

    pub fn due_pending(&self, now: i64) -> anyhow::Result<Vec<Pending>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, kind, payload, attempts, next_try_at FROM pending WHERE next_try_at <= ?1 ORDER BY id",
        )?;
        let rows = stmt.query_map(params![now], |r| {
            Ok(Pending {
                id: r.get(0)?,
                kind: r.get(1)?,
                payload: r.get(2)?,
                attempts: r.get::<_, i64>(3)? as u32,
                next_try_at: r.get(4)?,
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    pub fn pending_count(&self) -> anyhow::Result<i64> {
        Ok(self.lock().query_row("SELECT COUNT(*) FROM pending", [], |r| r.get(0))?)
    }

    pub fn mark_failed(&self, id: i64, next_try_at: i64) -> anyhow::Result<()> {
        self.lock().execute(
            "UPDATE pending SET attempts = attempts + 1, next_try_at = ?2 WHERE id = ?1",
            params![id, next_try_at],
        )?;
        Ok(())
    }

    pub fn remove_pending(&self, id: i64) -> anyhow::Result<()> {
        self.lock().execute("DELETE FROM pending WHERE id = ?1", params![id])?;
        Ok(())
    }

    // ---- meta ------------------------------------------------------------

    pub fn set_meta(&self, key: &str, value: &str) -> anyhow::Result<()> {
        self.lock().execute(
            "INSERT INTO meta(key, value) VALUES(?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn get_meta(&self, key: &str) -> anyhow::Result<Option<String>> {
        Ok(self
            .lock()
            .query_row("SELECT value FROM meta WHERE key = ?1", params![key], |r| r.get(0))
            .optional()?)
    }

    pub fn all_meta(&self) -> anyhow::Result<Vec<(String, String)>> {
        let conn = self.lock();
        let mut stmt = conn.prepare("SELECT key, value FROM meta ORDER BY key")?;
        let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upsert_then_known_hashes() {
        let s = State::open_in_memory().unwrap();
        s.upsert_hashes(&[("1".into(), "a".into()), ("2".into(), "b".into())]).unwrap();
        s.upsert_hashes(&[("2".into(), "c".into())]).unwrap();
        let k = s.known_hashes().unwrap();
        assert_eq!(k.len(), 2);
        assert_eq!(k["2"], "c");
        assert_eq!(s.count_items().unwrap(), 2);
        s.remove_items(&["1".into()]).unwrap();
        assert_eq!(s.count_items().unwrap(), 1);
    }

    #[test]
    fn enqueue_due_and_backoff() {
        let s = State::open_in_memory().unwrap();
        let now = 1_000;
        let id = s.enqueue(KIND_CATALOG, "{}").unwrap();
        let due = s.due_pending(now_unix()).unwrap();
        assert_eq!(due.len(), 1);
        assert_eq!(due[0].id, id);
        assert_eq!(due[0].attempts, 0);

        s.mark_failed(id, now + 30).unwrap();
        assert!(s.due_pending(now).unwrap().is_empty());
        let later = s.due_pending(now + 30).unwrap();
        assert_eq!(later.len(), 1);
        assert_eq!(later[0].attempts, 1);
        assert_eq!(s.pending_count().unwrap(), 1);

        s.remove_pending(id).unwrap();
        assert_eq!(s.pending_count().unwrap(), 0);
    }

    #[test]
    fn meta_roundtrip() {
        let s = State::open_in_memory().unwrap();
        assert_eq!(s.get_meta(META_ERP_STATUS).unwrap(), None);
        s.set_meta(META_ERP_STATUS, "ok").unwrap();
        s.set_meta(META_ERP_STATUS, "inalcanzable").unwrap();
        assert_eq!(s.get_meta(META_ERP_STATUS).unwrap().as_deref(), Some("inalcanzable"));
        assert_eq!(s.all_meta().unwrap().len(), 1);
    }

    #[test]
    fn open_creates_file_and_reopens() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("data").join("state.sqlite");
        {
            let s = State::open(&p).unwrap();
            s.upsert_hashes(&[("7454".into(), "h".into())]).unwrap();
        }
        assert!(p.exists());
        let s = State::open(&p).unwrap();
        assert_eq!(s.known_hashes().unwrap()["7454"], "h");
    }
}

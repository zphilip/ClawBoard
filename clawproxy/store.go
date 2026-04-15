package main

// store.go — v4 SQLite-backed offline queue.
//
// A single SQLite database buffers frames for all session types when the app
// is offline.  Frames survive proxy restarts and are evicted by TTL.
//
// Table schema:
//   queue(id INTEGER PK AUTOINCREMENT,
//         sess TEXT,      — session key (e.g. "ip-192.168.1.5" or bearer token)
//         agent TEXT,     — "zc" | "pc" | "" (compat sessions have no agent field)
//         sid TEXT,       — inner session_id for /proxy/ws; "" for compat sessions
//         data BLOB,      — raw JSON frame bytes
//         enqueued INT)   — unix timestamp (seconds)

import (
	"database/sql"
	"fmt"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

const dbSchema = `
CREATE TABLE IF NOT EXISTS queue (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sess     TEXT    NOT NULL,
    agent    TEXT    NOT NULL DEFAULT '',
    sid      TEXT    NOT NULL DEFAULT '',
    data     BLOB    NOT NULL,
    enqueued INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_sess ON queue(sess, id);
`

// storedMsg is one row read back from the queue table.
type storedMsg struct {
	agent string
	sid   string
	data  []byte
}

// queueStore wraps a SQLite database for buffered frame storage.
// It is safe for concurrent use.
type queueStore struct {
	db   *sql.DB
	maxQ int           // max rows per session key (0 = buffering disabled)
	ttl  time.Duration // row TTL; 0 = no expiry
	mu   sync.Mutex    // serialises all writes
	path string
}

// openQueueStore opens (or creates) the SQLite database at path and returns a
// ready-to-use queueStore.  Use path=":memory:" for an in-memory DB that
// behaves like v3 but still benefits from TTL.
func openQueueStore(path string, maxQ int, ttl time.Duration) (*queueStore, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open queue db %s: %w", path, err)
	}
	// WAL + relaxed sync: better write throughput on SD cards without data loss
	// on clean shutdown (power-cut can still lose the last WAL frame, same as v3).
	if _, err := db.Exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;`); err != nil {
		db.Close()
		return nil, fmt.Errorf("queue db pragmas: %w", err)
	}
	if _, err := db.Exec(dbSchema); err != nil {
		db.Close()
		return nil, fmt.Errorf("queue db schema: %w", err)
	}
	qs := &queueStore{db: db, maxQ: maxQ, ttl: ttl, path: path}
	go qs.purgeLoop()
	return qs, nil
}

// push buffers a frame for a session.
// Expired rows for this session are removed first, then the new row is inserted,
// then if over maxQ the oldest rows are dropped.
// No-op when maxQ == 0 (buffering disabled).
func (qs *queueStore) push(sess, agent, sid string, data []byte) {
	if qs.maxQ == 0 {
		return
	}
	now := time.Now().Unix()
	qs.mu.Lock()
	defer qs.mu.Unlock()

	// Evict expired rows for this session.
	if qs.ttl > 0 {
		cutoff := now - int64(qs.ttl.Seconds())
		qs.db.Exec("DELETE FROM queue WHERE sess=? AND enqueued<?", sess, cutoff) //nolint:errcheck
	}

	// Insert new frame.
	qs.db.Exec( //nolint:errcheck
		"INSERT INTO queue(sess,agent,sid,data,enqueued) VALUES(?,?,?,?,?)",
		sess, agent, sid, data, now)

	// Enforce per-session cap: keep only the newest maxQ rows.
	qs.db.Exec( //nolint:errcheck
		`DELETE FROM queue WHERE sess=? AND id NOT IN (
			SELECT id FROM queue WHERE sess=? ORDER BY id DESC LIMIT ?
		)`, sess, sess, qs.maxQ)
}

// drain returns and deletes all buffered frames for sess, oldest first.
func (qs *queueStore) drain(sess string) []storedMsg {
	qs.mu.Lock()
	defer qs.mu.Unlock()

	rows, err := qs.db.Query(
		"SELECT agent, sid, data FROM queue WHERE sess=? ORDER BY id ASC", sess)
	if err != nil {
		return nil
	}
	var msgs []storedMsg
	for rows.Next() {
		var m storedMsg
		if err := rows.Scan(&m.agent, &m.sid, &m.data); err == nil {
			msgs = append(msgs, m)
		}
	}
	rows.Close()
	if len(msgs) > 0 {
		qs.db.Exec("DELETE FROM queue WHERE sess=?", sess) //nolint:errcheck
	}
	return msgs
}

// count returns the current number of buffered frames for sess.
func (qs *queueStore) count(sess string) int {
	var n int
	qs.db.QueryRow("SELECT COUNT(*) FROM queue WHERE sess=?", sess).Scan(&n) //nolint:errcheck
	return n
}

// purgeLoop runs every 5 minutes and evicts all globally expired rows.
func (qs *queueStore) purgeLoop() {
	if qs.ttl <= 0 {
		return
	}
	t := time.NewTicker(5 * time.Minute)
	defer t.Stop()
	for range t.C {
		cutoff := time.Now().Add(-qs.ttl).Unix()
		qs.mu.Lock()
		res, err := qs.db.Exec("DELETE FROM queue WHERE enqueued<?", cutoff)
		qs.mu.Unlock()
		if err == nil {
			if n, _ := res.RowsAffected(); n > 0 {
				fmt.Printf("%squeue TTL purge: %d expired rows removed\n", prefixSYS(), n)
			}
		}
	}
}

// close shuts down the database connection.
func (qs *queueStore) close() {
	qs.db.Close()
}

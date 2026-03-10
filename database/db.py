"""
DATABASE — Stage 6
SQLite database: trades, signals, capital log, performance.
"""

import sqlite3, json, datetime, os

DB_PATH = os.environ.get("DB_PATH", "/tmp/quantra.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c    = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT UNIQUE,
        timestamp TEXT,
        symbol TEXT,
        action TEXT,
        composite_score REAL,
        confidence REAL,
        engine_scores TEXT,
        top_reasons TEXT,
        market_data TEXT,
        was_taken INTEGER DEFAULT 0,
        result TEXT DEFAULT 'OPEN'
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT,
        symbol TEXT,
        action TEXT,
        ce_strike INTEGER,
        pe_strike INTEGER,
        lots INTEGER,
        ce_entry_price REAL,
        pe_entry_price REAL,
        ce_exit_price REAL,
        pe_exit_price REAL,
        capital_used INTEGER,
        max_loss INTEGER,
        target_pnl INTEGER,
        actual_pnl REAL,
        exit_type TEXT,
        entry_time TEXT,
        exit_time TEXT,
        capital_before REAL,
        capital_after REAL,
        notes TEXT
    );
    CREATE TABLE IF NOT EXISTS capital_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        event TEXT,
        amount REAL,
        balance REAL,
        note TEXT
    );
    CREATE TABLE IF NOT EXISTS performance_daily (
        date TEXT PRIMARY KEY,
        total_signals INTEGER,
        signals_taken INTEGER,
        wins INTEGER,
        losses INTEGER,
        net_pnl REAL,
        win_rate REAL,
        best_engine TEXT,
        engine_scores TEXT
    );
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Initialized at {DB_PATH}")

def save_signal(signal: dict):
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO signals
        (signal_id, timestamp, symbol, action, composite_score, confidence,
         engine_scores, top_reasons, market_data)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        signal.get("signal_id"), signal.get("timestamp"), signal.get("symbol"),
        signal.get("action"), signal.get("composite"), signal.get("confidence"),
        json.dumps(signal.get("engines",{})),
        json.dumps(signal.get("top_reasons",[])),
        json.dumps(signal.get("market_data",{})),
    ))
    conn.commit(); conn.close()

def save_trade(trade: dict):
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (signal_id, symbol, action, ce_strike, pe_strike, lots,
         ce_entry_price, pe_entry_price, capital_used, max_loss, target_pnl,
         entry_time, capital_before)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        trade.get("signal_id"), trade.get("symbol"), trade.get("action"),
        trade.get("ce_strike"), trade.get("pe_strike"), trade.get("lots"),
        trade.get("ce_price"), trade.get("pe_price"),
        trade.get("capital_used"), trade.get("max_loss"), trade.get("target_pnl"),
        datetime.datetime.now().isoformat(), trade.get("capital_before",0),
    ))
    trade_id = c.lastrowid
    conn.commit(); conn.close()
    return trade_id

def close_trade(trade_id: int, exit_data: dict):
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        UPDATE trades SET
            ce_exit_price=?, pe_exit_price=?, actual_pnl=?,
            exit_type=?, exit_time=?, capital_after=?, notes=?
        WHERE id=?
    """, (
        exit_data.get("ce_exit_price"), exit_data.get("pe_exit_price"),
        exit_data.get("pnl"), exit_data.get("exit_type"),
        datetime.datetime.now().isoformat(), exit_data.get("capital_after"),
        exit_data.get("notes",""), trade_id
    ))
    conn.commit(); conn.close()

def get_trade_history(limit=50):
    conn = get_conn()
    c    = conn.cursor()
    rows = c.execute("""
        SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_performance_summary():
    conn = get_conn()
    c    = conn.cursor()
    row  = c.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN actual_pnl <= 0 THEN 1 ELSE 0 END) as losses,
            SUM(actual_pnl) as net_pnl,
            AVG(CASE WHEN actual_pnl > 0 THEN actual_pnl END) as avg_win,
            AVG(CASE WHEN actual_pnl < 0 THEN actual_pnl END) as avg_loss,
            MAX(actual_pnl) as best_trade,
            MIN(actual_pnl) as worst_trade
        FROM trades WHERE actual_pnl IS NOT NULL
    """).fetchone()
    conn.close()
    if not row: return {}
    r = dict(row)
    r["win_rate"] = round(r["wins"]/r["total_trades"]*100,1) if r["total_trades"] > 0 else 0
    return r

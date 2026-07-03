import sqlite3
from datetime import datetime, timedelta
import pandas as pd
import json
import os
import re
import time
import secrets
import hashlib
import hmac
from werkzeug.security import generate_password_hash, check_password_hash

# Sentinel: omit ticket_unit_php from UPDATE when unchanged
_NO_TICKET_UNIT_UPDATE = object()


class Database:
    # Rotating default icons for new user-added categories without icons
    DEFAULT_CATEGORY_ICONS = ['🏷️', '💼', '🧾', '🧮', '🏢', '📦', '🗂️', '🧷', '🛠️', '📌']
    # Reserved ticket_pads.source for stubs not yet assigned to an income category
    TICKET_PAD_POOL_SOURCE = "__STUB_POOL__"

    def __init__(self, db_name='meedo_revenue.db'):
        self.db_name = db_name
        self.init_database()
        self.migrate_schema_if_needed()

    def _connect(self):
        """
        Create a SQLite connection configured for better concurrency.
        WAL + busy_timeout reduces 'database is locked' errors when background
        initialization is writing while UI requests are coming in.
        """
        conn = sqlite3.connect(self.db_name, timeout=10)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA busy_timeout=5000')
        except Exception:
            pass
        return conn
    
    def init_database(self):
        """Initialize database with required tables"""
        conn = self._connect()
        cursor = conn.cursor()
        
        # Create tables if they don't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS historical_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                amount_remitted FLOAT NOT NULL,
                source VARCHAR(50) NOT NULL,
                section VARCHAR(50),
                year INTEGER,
                month INTEGER
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_income_inputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                income_date DATE NOT NULL,
                source VARCHAR(50) NOT NULL,
                monthly_income FLOAT NOT NULL,
                notes TEXT,
                created_by VARCHAR(50) DEFAULT 'user'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                source VARCHAR(50) NOT NULL,
                input_income FLOAT NOT NULL,
                predicted_monthly FLOAT,
                predicted_yearly FLOAT,
                confidence_score FLOAT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                report_type VARCHAR(20),
                source VARCHAR(50),
                start_date DATE,
                end_date DATE,
                total_income FLOAT,
                avg_monthly FLOAT,
                projected_yearly FLOAT,
                report_data TEXT
            )
        ''')
        
        # Categories (income sources) - allows user to add new income categories
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id VARCHAR(60) PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT DEFAULT '',
                ticket_unit_php REAL DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Users / authentication (simple session-based auth)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff',
                is_active INTEGER NOT NULL DEFAULT 1,
                session_version INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login DATETIME
            )
        ''')

        # Password reset tokens (email-based)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                sent_to TEXT,
                expires_at DATETIME NOT NULL,
                used_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_user_id ON password_resets(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_expires_at ON password_resets(expires_at)")
        except Exception:
            pass

        # Roles (admin can manage)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_unique_name ON roles(LOWER(TRIM(name)))")
        except Exception:
            pass

        # Ticket pads (numbered books / stubs with finite ticket counts)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_pads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                book_number TEXT NOT NULL,
                ticket_count INTEGER NOT NULL,
                pad_value_php REAL NOT NULL,
                tickets_consumed INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, book_number)
            )
        ''')
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticket_pads_source ON ticket_pads(source)")
        except Exception:
            pass

        # Tracker monthly entries (shared across users; source/year/month unique)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tracker_monthly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                amount FLOAT NOT NULL,
                first_amount FLOAT,
                prev_amount FLOAT,
                tickets_sold INTEGER,
                ticket_pad_id INTEGER,
                created_by TEXT DEFAULT 'user',
                created_role TEXT DEFAULT 'staff',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT,
                updated_role TEXT,
                updated_at DATETIME
            )
        ''')
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracker_monthly_unique "
                "ON tracker_monthly(source, year, month)"
            )
        except Exception:
            pass

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tracker_daily_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                peso_json TEXT NOT NULL DEFAULT '{}',
                ticket_json TEXT NOT NULL DEFAULT '{}',
                peso_audit_json TEXT NOT NULL DEFAULT '{}',
                ticket_audit_json TEXT NOT NULL DEFAULT '{}',
                updated_by TEXT,
                updated_role TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, year, month)
            )
            """
        )
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracker_daily_unique "
                "ON tracker_daily_detail(source, year, month)"
            )
        except Exception:
            pass
        
        # Seed default categories (only if empty)
        self.init_default_categories(cursor)

        # Seed default roles (only if empty)
        try:
            cursor.execute("SELECT COUNT(*) FROM roles")
            n_roles = int(cursor.fetchone()[0] or 0)
            if n_roles == 0:
                cursor.execute("INSERT INTO roles (name, is_admin, is_active) VALUES ('admin', 1, 1)")
                cursor.execute("INSERT INTO roles (name, is_admin, is_active) VALUES ('staff', 0, 1)")
        except Exception:
            pass

        # Seed default admin user (only if users table is empty)
        self._init_default_admin_user(cursor)

        # Ensure new columns exist on older DB files
        try:
            self._ensure_users_session_version(cursor)
        except Exception:
            pass
        # Ensure security questions columns exist on older DB files
        try:
            self._ensure_users_security_questions(cursor)
        except Exception:
            pass
        # Ensure email column and unique index exist on older DB files
        try:
            self._ensure_users_email(cursor)
        except Exception:
            pass
        
        conn.commit()
        conn.close()
        
        print(f"[OK] Database initialized/verified: {self.db_name}")

    def _ensure_users_session_version(self, cursor):
        """Add users.session_version column for existing DBs."""
        cursor.execute("PRAGMA table_info(users)")
        cols = [str(r[1] or '').lower() for r in (cursor.fetchall() or [])]
        if 'session_version' in cols:
            return
        cursor.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        cursor.execute("UPDATE users SET session_version = 0 WHERE session_version IS NULL")

    def _ensure_users_security_questions(self, cursor):
        """
        Add security questions columns for existing DBs.
        We store:
          - sec_q1, sec_q2: question ids/keys (TEXT)
          - sec_a1_hash, sec_a2_hash: hashed answers (TEXT)
        """
        cursor.execute("PRAGMA table_info(users)")
        cols = [str(r[1] or '').lower() for r in (cursor.fetchall() or [])]
        adds = []
        if 'sec_q1' not in cols:
            adds.append("ALTER TABLE users ADD COLUMN sec_q1 TEXT")
        if 'sec_a1_hash' not in cols:
            adds.append("ALTER TABLE users ADD COLUMN sec_a1_hash TEXT")
        if 'sec_q2' not in cols:
            adds.append("ALTER TABLE users ADD COLUMN sec_q2 TEXT")
        if 'sec_a2_hash' not in cols:
            adds.append("ALTER TABLE users ADD COLUMN sec_a2_hash TEXT")
        for stmt in adds:
            cursor.execute(stmt)

    def _ensure_users_email(self, cursor):
        """
        Add users.email column for existing DBs and enforce uniqueness (case-insensitive).
        Allows NULL/blank values; uniqueness enforced only for non-empty emails.
        """
        cursor.execute("PRAGMA table_info(users)")
        cols = [str(r[1] or '').lower() for r in (cursor.fetchall() or [])]
        if 'email' not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
        # Create a partial unique index (SQLite >= 3.8.0 supports partial indexes)
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_unique_email "
                "ON users(LOWER(TRIM(email))) "
                "WHERE email IS NOT NULL AND TRIM(email) <> ''"
            )
        except Exception:
            pass

    # ---------------------------
    # Security Questions (forgot password)
    # ---------------------------
    def get_security_questions_by_username(self, username: str):
        """
        Returns:
          - None if user not found
          - dict { q1, q2, is_configured: bool }
        """
        u = str(username or '').strip()
        if not u:
            return None
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT sec_q1, sec_q2, sec_a1_hash, sec_a2_hash FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1",
            (u,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        q1, q2, a1h, a2h = row
        configured = bool(q1 and q2 and a1h and a2h)
        return {"q1": q1, "q2": q2, "is_configured": configured}

    def get_security_questions_by_user_id(self, user_id: int):
        """
        Returns:
          - None if user not found
          - dict { q1, q2, is_configured: bool }
        """
        uid = int(user_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT sec_q1, sec_q2, sec_a1_hash, sec_a2_hash FROM users WHERE id = ? LIMIT 1",
            (uid,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        q1, q2, a1h, a2h = row
        configured = bool(q1 and q2 and a1h and a2h)
        return {"q1": q1, "q2": q2, "is_configured": configured}

    def set_security_questions(self, user_id: int, q1: str, a1: str, q2: str, a2: str):
        """
        Set/replace the security questions and answers for a user.
        Answers are hashed using werkzeug (same hashing used for passwords).
        """
        uid = int(user_id)
        q1k = str(q1 or '').strip()
        q2k = str(q2 or '').strip()
        a1s = str(a1 or '').strip()
        a2s = str(a2 or '').strip()
        if not q1k or not q2k:
            raise ValueError("Security questions are required")
        if q1k == q2k:
            raise ValueError("Security questions must be different")
        if len(a1s) < 2 or len(a2s) < 2:
            raise ValueError("Answers are too short")
        # Normalize answers before hashing so verification can be case/space-insensitive.
        a1h = generate_password_hash(self._normalize_answer(a1s))
        a2h = generate_password_hash(self._normalize_answer(a2s))
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET sec_q1 = ?, sec_a1_hash = ?, sec_q2 = ?, sec_a2_hash = ? WHERE id = ?",
            (q1k, a1h, q2k, a2h, uid),
        )
        conn.commit()
        conn.close()
        return True

    def _normalize_answer(self, s: str) -> str:
        # Trim and collapse spaces; keep case-insensitive matching by lowercasing
        t = str(s or '').strip().lower()
        t = re.sub(r'\s+', ' ', t)
        return t

    def _answer_variants(self, s: str):
        """
        Provide a few backward-compatible variants for verification.
        This allows accounts created before normalization to still pass.
        """
        raw = str(s or '').strip()
        norm = self._normalize_answer(raw)
        low = raw.lower()
        low = re.sub(r'\s+', ' ', low).strip()
        # Keep order: preferred normalized first
        seen = set()
        out = []
        for v in (norm, raw, low):
            if not v:
                continue
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out

    def verify_security_answers(self, username: str, a1: str, a2: str) -> bool:
        u = str(username or '').strip()
        if not u:
            return False
        a1_variants = self._answer_variants(a1)
        a2_variants = self._answer_variants(a2)
        if not a1_variants or not a2_variants:
            return False
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT sec_a1_hash, sec_a2_hash, sec_q1, sec_q2, is_active FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1",
            (u,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return False
        a1h, a2h, q1, q2, is_active = row
        if int(is_active or 0) != 1:
            # Disabled users cannot self-reset; admin must handle it
            return False
        if not (q1 and q2 and a1h and a2h):
            return False
        try:
            ok1 = any(check_password_hash(a1h, v) for v in a1_variants)
            ok2 = any(check_password_hash(a2h, v) for v in a2_variants)
        except Exception:
            return False
        return bool(ok1 and ok2)

    def reset_password_by_security(self, username: str, a1: str, a2: str, new_password: str):
        u = str(username or '').strip()
        np = str(new_password or '').strip()
        if not u:
            raise ValueError("Username is required")
        if not np or len(np) < 4:
            raise ValueError("New password must be at least 4 characters")
        if not self.verify_security_answers(u, a1, a2):
            raise ValueError("Security answers are incorrect (or account is disabled).")
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1", (u,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found")
        uid = int(row[0])
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(np), uid))
        # Invalidate existing sessions
        cursor.execute("UPDATE users SET session_version = COALESCE(session_version, 0) + 1 WHERE id = ?", (uid,))
        conn.commit()
        conn.close()
        return True

    def _init_default_admin_user(self, cursor):
        """
        Create a first admin account if `users` is empty.
        Uses environment variables when available:
          - MEEDO_ADMIN_USERNAME (default: admin)
          - MEEDO_ADMIN_PASSWORD (required to auto-create; if missing we create a disabled placeholder)
        """
        try:
            cursor.execute('SELECT COUNT(*) FROM users')
            n = int(cursor.fetchone()[0] or 0)
            if n != 0:
                return

            username = str(os.environ.get('MEEDO_ADMIN_USERNAME', 'admin') or 'admin').strip()
            password = str(os.environ.get('MEEDO_ADMIN_PASSWORD', '') or '').strip()
            if password:
                ph = generate_password_hash(password)
                cursor.execute(
                    'INSERT INTO users (username, password_hash, role, is_active) VALUES (?, ?, ?, 1)',
                    (username, ph, 'admin')
                )
                print(f"[OK] Default admin user created: {username}")
            else:
                # Create a placeholder disabled admin so the app can guide the user to set env vars.
                ph = generate_password_hash('CHANGE_ME')
                cursor.execute(
                    'INSERT INTO users (username, password_hash, role, is_active) VALUES (?, ?, ?, 0)',
                    (username, ph, 'admin')
                )
                print("[WARN] Admin user created but DISABLED (set MEEDO_ADMIN_PASSWORD then re-enable or reset).")
        except Exception as e:
            print(f"[WARN] Could not init default admin user: {e}")
    
    def migrate_schema_if_needed(self):
        """Check and add any missing columns to existing tables"""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            
            # Check if user_income_inputs has all required columns
            cursor.execute("PRAGMA table_info(user_income_inputs)")
            columns = [col[1] for col in cursor.fetchall()]
            
            # Add missing columns if needed
            if 'notes' not in columns:
                cursor.execute("ALTER TABLE user_income_inputs ADD COLUMN notes TEXT")
                print("[OK] Added 'notes' column to user_income_inputs")
            
            if 'created_by' not in columns:
                cursor.execute("ALTER TABLE user_income_inputs ADD COLUMN created_by VARCHAR(50) DEFAULT 'user'")
                print("[OK] Added 'created_by' column to user_income_inputs")

            # Enforce unique category names (case-insensitive) at the DB level
            # This prevents duplicates even if older code paths attempt to insert them.
            try:
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_unique_name_active "
                    "ON categories(LOWER(TRIM(name))) WHERE is_active = 1"
                )
            except Exception as e:
                print(f"[WARN] Could not create categories unique index: {e}")

            # Per-category ticket unit price (for revenue = tickets × price)
            try:
                cursor.execute("PRAGMA table_info(categories)")
                cat_cols = [str(r[1] or '').lower() for r in (cursor.fetchall() or [])]
                if 'ticket_unit_php' not in cat_cols:
                    cursor.execute(
                        "ALTER TABLE categories ADD COLUMN ticket_unit_php REAL DEFAULT 0"
                    )
                    print("[OK] Added ticket_unit_php column to categories")
            except Exception as e:
                print(f"[WARN] Could not add categories.ticket_unit_php: {e}")

            # Ticket pads + tracker ticket columns (older DB files)
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ticket_pads'")
                if not cursor.fetchone():
                    cursor.execute(
                        """
                        CREATE TABLE ticket_pads (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            source TEXT NOT NULL,
                            book_number TEXT NOT NULL,
                            ticket_count INTEGER NOT NULL,
                            pad_value_php REAL NOT NULL,
                            tickets_consumed INTEGER NOT NULL DEFAULT 0,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE(source, book_number)
                        )
                        """
                    )
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticket_pads_source ON ticket_pads(source)")
                    print("[OK] Created ticket_pads table")
                cursor.execute("PRAGMA table_info(tracker_monthly)")
                tm_cols = [str(r[1] or '').lower() for r in (cursor.fetchall() or [])]
                if 'tickets_sold' not in tm_cols:
                    cursor.execute("ALTER TABLE tracker_monthly ADD COLUMN tickets_sold INTEGER")
                    print("[OK] Added tracker_monthly.tickets_sold")
                if 'ticket_pad_id' not in tm_cols:
                    cursor.execute("ALTER TABLE tracker_monthly ADD COLUMN ticket_pad_id INTEGER")
                    print("[OK] Added tracker_monthly.ticket_pad_id")
            except Exception as e:
                print(f"[WARN] Ticket pad / tracker column migration: {e}")
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Schema migration issue (non-critical): {e}")

        # Remove legacy toilet-upgrade lookup table if present
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute('DROP TABLE IF EXISTS toilet_upgrade_thresholds')
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Could not drop legacy toilet_upgrade_thresholds: {e}")

        # Users table migration / index safety
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'staff', is_active INTEGER NOT NULL DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_login DATETIME)")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Users table migration issue (non-critical): {e}")

        # Roles table migration / seed
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS roles ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL UNIQUE, "
                "is_admin INTEGER NOT NULL DEFAULT 0, "
                "is_active INTEGER NOT NULL DEFAULT 1, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_unique_name ON roles(LOWER(TRIM(name)))")
            # Ensure base roles exist
            cursor.execute("INSERT OR IGNORE INTO roles (name, is_admin, is_active) VALUES ('admin', 1, 1)")
            cursor.execute("INSERT OR IGNORE INTO roles (name, is_admin, is_active) VALUES ('staff', 0, 1)")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Roles table migration issue (non-critical): {e}")

        # Tracker monthly table migration / index safety
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS tracker_monthly ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "source TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL, "
                "amount FLOAT NOT NULL, first_amount FLOAT, prev_amount FLOAT, "
                "created_by TEXT DEFAULT 'user', created_role TEXT DEFAULT 'staff', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_by TEXT, updated_role TEXT, updated_at DATETIME)"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracker_monthly_unique "
                "ON tracker_monthly(source, year, month)"
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] Tracker monthly table migration issue (non-critical): {e}")

        # Shared daily breakdown (peso + ticket counts per calendar day) for tracker months
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tracker_daily_detail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    peso_json TEXT NOT NULL DEFAULT '{}',
                    ticket_json TEXT NOT NULL DEFAULT '{}',
                    updated_by TEXT,
                    updated_role TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source, year, month)
                )
                """
            )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracker_daily_unique "
                "ON tracker_daily_detail(source, year, month)"
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] tracker_daily_detail migration issue (non-critical): {e}")

        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(tracker_daily_detail)")
            tdd_cols = [str(r[1] or "").lower() for r in (cursor.fetchall() or [])]
            if "peso_audit_json" not in tdd_cols:
                cursor.execute(
                    "ALTER TABLE tracker_daily_detail ADD COLUMN peso_audit_json TEXT NOT NULL DEFAULT '{}'"
                )
                print("[OK] Added tracker_daily_detail.peso_audit_json")
            if "ticket_audit_json" not in tdd_cols:
                cursor.execute(
                    "ALTER TABLE tracker_daily_detail ADD COLUMN ticket_audit_json TEXT NOT NULL DEFAULT '{}'"
                )
                print("[OK] Added tracker_daily_detail.ticket_audit_json")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] tracker_daily_detail audit column migration: {e}")

        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(tracker_monthly)")
            tm_rm = [str(r[1] or "").lower() for r in (cursor.fetchall() or [])]
            if "revenue_entry_mode" not in tm_rm:
                cursor.execute("ALTER TABLE tracker_monthly ADD COLUMN revenue_entry_mode TEXT")
                print("[OK] Added tracker_monthly.revenue_entry_mode")
            cursor.execute(
                "UPDATE tracker_monthly SET revenue_entry_mode = NULL "
                "WHERE revenue_entry_mode IS NOT NULL "
                "AND LOWER(TRIM(revenue_entry_mode)) NOT IN ('monthly', 'daily')"
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[WARN] tracker_monthly revenue_entry_mode migration: {e}")

    def _sanitize_tracker_day_map(self, raw):
        """Keep only day keys 1–31 with non-negative integers (matches JS whole-number inputs)."""
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            sk = str(k).strip()
            if not sk.isdigit():
                continue
            d = int(sk)
            if d < 1 or d > 31:
                continue
            try:
                n = int(round(float(v)))
            except (TypeError, ValueError):
                continue
            if n < 0:
                continue
            out[str(d)] = n
        return out

    def _sum_positive_tracker_day_values(self, mp) -> int:
        """Sum of positive whole amounts in a day map (keys 1–31, non-int keys ignored)."""
        mp = mp if isinstance(mp, dict) else {}
        total = 0
        for _k, v in mp.items():
            try:
                n = int(round(float(v)))
            except (TypeError, ValueError):
                continue
            if n > 0:
                total += n
        return total

    def _enforce_staff_daily_no_net_decrease(self, old_map, new_map, label: str):
        """Staff may rebalance between days; they must not reduce the month total for this map."""
        old_sum = self._sum_positive_tracker_day_values(old_map)
        new_sum = self._sum_positive_tracker_day_values(new_map)
        if new_sum < old_sum:
            raise ValueError(
                f"Staff cannot reduce the total daily {label} for this month "
                f"(previous total {old_sum}; new total {new_sum}). "
                "You can move amounts between days if the total stays the same or increases, "
                "or ask an administrator to correct it."
            )

    def _load_tracker_month_cash_ticket_targets(self, cursor, src, y, m):
        """Return (implied_cash_php int or None, official_tickets int or None) from tracker_monthly + category unit."""
        try:
            cursor.execute(
                "SELECT tm.amount, tm.tickets_sold, COALESCE(c.ticket_unit_php, 0) "
                "FROM tracker_monthly tm "
                "LEFT JOIN categories c ON c.id = tm.source "
                "WHERE tm.source = ? AND tm.year = ? AND tm.month = ? LIMIT 1",
                (str(src).strip(), int(y), int(m)),
            )
            row = cursor.fetchone()
            if not row:
                return None, None
            amt = int(round(float(row[0] or 0)))
            raw_ts = row[1]
            unit = float(row[2] or 0)
            official_ts = int(raw_ts) if raw_ts is not None else None
            if official_ts is not None and official_ts > 0 and unit > 0:
                implied_cash = max(0, amt - int(round(official_ts * unit)))
            elif official_ts is None or official_ts <= 0:
                implied_cash = max(0, amt)
            else:
                implied_cash = None
            return implied_cash, official_ts
        except Exception:
            return None, None

    def _staff_peso_daily_allowed_after_month_row(self, old_map, new_map, implied_cash):
        """True if peso daily total can drop vs previous because it matches the saved month split."""
        if implied_cash is None:
            return False
        new_sum = self._sum_positive_tracker_day_values(new_map)
        return abs(int(new_sum) - int(implied_cash)) <= 1

    def _staff_ticket_daily_allowed_after_month_row(self, old_map, new_map, official_tickets):
        """True if ticket daily total can drop because it matches tickets_sold on the month row."""
        if official_tickets is None or official_tickets < 0:
            return False
        new_sum = self._sum_positive_tracker_day_values(new_map)
        return int(new_sum) == int(official_tickets)

    def _enforce_staff_daily_peso_with_month_anchor(self, old_map, new_map, implied_cash):
        old_sum = self._sum_positive_tracker_day_values(old_map)
        new_sum = self._sum_positive_tracker_day_values(new_map)
        if new_sum >= old_sum:
            return
        if self._staff_peso_daily_allowed_after_month_row(old_map, new_map, implied_cash):
            return
        self._enforce_staff_daily_no_net_decrease(old_map, new_map, "cash (pesos)")

    def _enforce_staff_daily_ticket_with_month_anchor(self, old_map, new_map, official_tickets):
        old_sum = self._sum_positive_tracker_day_values(old_map)
        new_sum = self._sum_positive_tracker_day_values(new_map)
        if new_sum >= old_sum:
            return
        if self._staff_ticket_daily_allowed_after_month_row(old_map, new_map, official_tickets):
            return
        self._enforce_staff_daily_no_net_decrease(old_map, new_map, "tickets")

    def _staff_merge_locked_day_audits(self, incoming_audit, old_audit, old_amounts, new_amounts=None):
        """Preserve attribution for unchanged days; allow new audit when staff only increases a day.

        new_amounts: sanitized incoming day map (peso or ticket) for the save. If omitted (legacy
        3-argument callers), only the previous server audit is kept for days that already had a value.
        """
        incoming_audit = self._sanitize_tracker_day_audit_map(
            incoming_audit if isinstance(incoming_audit, dict) else {}
        )
        old_audit = self._sanitize_tracker_day_audit_map(old_audit if isinstance(old_audit, dict) else {})
        old_amounts = self._sanitize_tracker_day_map(old_amounts if isinstance(old_amounts, dict) else {})
        out = dict(incoming_audit)
        if new_amounts is None:
            for k, n in old_amounts.items():
                sk = str(k).strip()
                if not sk.isdigit():
                    continue
                try:
                    if int(round(float(n))) <= 0:
                        continue
                except Exception:
                    continue
                if sk in old_audit:
                    out[sk] = old_audit[sk]
            return self._sanitize_tracker_day_audit_map(out)

        new_amounts = self._sanitize_tracker_day_map(new_amounts if isinstance(new_amounts, dict) else {})
        for k, n_old in old_amounts.items():
            sk = str(k).strip()
            if not sk.isdigit():
                continue
            try:
                o_old = int(round(float(n_old)))
            except Exception:
                continue
            if o_old <= 0:
                continue
            try:
                n_new = int(round(float(new_amounts.get(sk, new_amounts.get(k, 0)))))
            except Exception:
                n_new = 0
            if n_new > o_old:
                if sk in incoming_audit:
                    out[sk] = incoming_audit[sk]
                elif sk in old_audit:
                    out[sk] = old_audit[sk]
            elif sk in old_audit:
                out[sk] = old_audit[sk]
        return self._sanitize_tracker_day_audit_map(out)

    def _sanitize_tracker_day_audit_map(self, raw):
        """Day keys 1–31 -> {updated_by, updated_role} for per-row attribution."""
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            sk = str(k).strip()
            if not sk.isdigit():
                continue
            d = int(sk)
            if d < 1 or d > 31:
                continue
            if not isinstance(v, dict):
                continue
            ub = str(v.get("updated_by") or "").strip()[:120]
            ur = str(v.get("updated_role") or "").strip()[:80]
            if not ub and not ur:
                continue
            out[str(d)] = {
                "updated_by": ub or "user",
                "updated_role": ur or "staff",
            }
        return out

    def get_tracker_daily_for_year(self, source: str, year: int):
        """Return dict month_index (int) -> { 'peso': {...}, 'ticket': {...} } from shared storage."""
        src = str(source or "").strip()
        y = int(year)
        if not src:
            return {}
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT month, peso_json, ticket_json, peso_audit_json, ticket_audit_json, "
            "updated_by, updated_role, updated_at "
            "FROM tracker_daily_detail WHERE source = ? AND year = ?",
            (src, y),
        )
        rows = cursor.fetchall()
        conn.close()
        out = {}
        for r in rows:
            m = int(r[0])
            try:
                pj = json.loads(r[1] or "{}")
            except Exception:
                pj = {}
            try:
                tj = json.loads(r[2] or "{}")
            except Exception:
                tj = {}
            try:
                pja = json.loads(r[3] or "{}")
            except Exception:
                pja = {}
            try:
                tja = json.loads(r[4] or "{}")
            except Exception:
                tja = {}
            out[m] = {
                "peso": self._sanitize_tracker_day_map(pj if isinstance(pj, dict) else {}),
                "ticket": self._sanitize_tracker_day_map(tj if isinstance(tj, dict) else {}),
                "peso_audit": self._sanitize_tracker_day_audit_map(pja if isinstance(pja, dict) else {}),
                "ticket_audit": self._sanitize_tracker_day_audit_map(tja if isinstance(tja, dict) else {}),
                "updated_by": (r[5] if r[5] is not None else None),
                "updated_role": (r[6] if r[6] is not None else None),
                "updated_at": (r[7] if r[7] is not None else None),
            }
        return out

    def upsert_tracker_daily_month(
        self,
        source: str,
        year: int,
        month: int,
        peso_map,
        ticket_map,
        acting_username: str,
        acting_role: str,
        peso_audit_map=None,
        ticket_audit_map=None,
    ):
        """Replace daily peso/ticket day maps for one tracker month (shared across users)."""
        src = str(source or "").strip()
        y = int(year)
        m = int(month)
        if not src:
            raise ValueError("source is required")
        if m < 0 or m > 11:
            raise ValueError("month must be 0–11")
        peso = self._sanitize_tracker_day_map(peso_map if isinstance(peso_map, dict) else {})
        tick = self._sanitize_tracker_day_map(ticket_map if isinstance(ticket_map, dict) else {})
        pa = self._sanitize_tracker_day_audit_map(peso_audit_map if isinstance(peso_audit_map, dict) else {})
        ta = self._sanitize_tracker_day_audit_map(ticket_audit_map if isinstance(ticket_audit_map, dict) else {})
        by = str(acting_username or "user").strip() or "user"
        rl = str(acting_role or "staff").strip() or "staff"
        conn = self._connect()
        cursor = conn.cursor()
        old_peso = {}
        old_tick = {}
        old_pa = {}
        old_ta = {}
        try:
            cursor.execute(
                "SELECT peso_json, ticket_json, peso_audit_json, ticket_audit_json "
                "FROM tracker_daily_detail WHERE source = ? AND year = ? AND month = ?",
                (src, y, m),
            )
            ex = cursor.fetchone()
            if ex:
                try:
                    pj0 = json.loads(ex[0] or "{}")
                except Exception:
                    pj0 = {}
                try:
                    tj0 = json.loads(ex[1] or "{}")
                except Exception:
                    tj0 = {}
                try:
                    pja0 = json.loads(ex[2] or "{}")
                except Exception:
                    pja0 = {}
                try:
                    tja0 = json.loads(ex[3] or "{}")
                except Exception:
                    tja0 = {}
                old_peso = self._sanitize_tracker_day_map(pj0 if isinstance(pj0, dict) else {})
                old_tick = self._sanitize_tracker_day_map(tj0 if isinstance(tj0, dict) else {})
                old_pa = self._sanitize_tracker_day_audit_map(pja0 if isinstance(pja0, dict) else {})
                old_ta = self._sanitize_tracker_day_audit_map(tja0 if isinstance(tja0, dict) else {})
            is_admin = self.is_role_admin(rl)
            if not is_admin:
                # Overlay client maps onto stored maps. Otherwise omitted day keys look like 0 and
                # staff enforcement rejects them as "decrease/clear" even when the client only
                # meant to update other days (partial payload / tab mismatch).
                merged_p = dict(old_peso)
                for k, v in peso.items():
                    try:
                        nk = int(round(float(v)))
                    except (TypeError, ValueError):
                        continue
                    if nk <= 0:
                        # Treat non-positive payloads as omitted days (preserve server value).
                        continue
                    merged_p[k] = nk
                peso = self._sanitize_tracker_day_map(merged_p)
                merged_t = dict(old_tick)
                for k, v in tick.items():
                    try:
                        nk = int(round(float(v)))
                    except (TypeError, ValueError):
                        continue
                    if nk <= 0:
                        continue
                    merged_t[k] = nk
                tick = self._sanitize_tracker_day_map(merged_t)
                implied_cash, official_ts = self._load_tracker_month_cash_ticket_targets(
                    cursor, src, y, m
                )
                self._enforce_staff_daily_peso_with_month_anchor(old_peso, peso, implied_cash)
                self._enforce_staff_daily_ticket_with_month_anchor(old_tick, tick, official_ts)
                pa = self._staff_merge_locked_day_audits(pa, old_pa, old_peso, peso)
                ta = self._staff_merge_locked_day_audits(ta, old_ta, old_tick, tick)
        except ValueError:
            try:
                conn.close()
            except Exception:
                pass
            raise
        pj = json.dumps(peso, separators=(",", ":"))
        tj = json.dumps(tick, separators=(",", ":"))
        paj = json.dumps(pa, separators=(",", ":"))
        taj = json.dumps(ta, separators=(",", ":"))
        try:
            cursor.execute(
                "SELECT id FROM tracker_daily_detail WHERE source = ? AND year = ? AND month = ?",
                (src, y, m),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE tracker_daily_detail SET peso_json = ?, ticket_json = ?, "
                    "peso_audit_json = ?, ticket_audit_json = ?, "
                    "updated_by = ?, updated_role = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (pj, tj, paj, taj, by, rl, int(row[0])),
                )
            else:
                cursor.execute(
                    "INSERT INTO tracker_daily_detail (source, year, month, peso_json, ticket_json, "
                    "peso_audit_json, ticket_audit_json, updated_by, updated_role) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (src, y, m, pj, tj, paj, taj, by, rl),
                )
            try:
                cursor.execute(
                    "UPDATE tracker_monthly SET revenue_entry_mode = ? "
                    "WHERE source = ? AND year = ? AND month = ?",
                    ("daily", src, y, m),
                )
            except Exception:
                pass
            conn.commit()
            cursor.execute(
                "SELECT updated_at, updated_by, updated_role FROM tracker_daily_detail "
                "WHERE source = ? AND year = ? AND month = ?",
                (src, y, m),
            )
            aud = cursor.fetchone()
            ua = aud[0] if aud else None
            ub = aud[1] if aud and aud[1] is not None else by
            ur = aud[2] if aud and aud[2] is not None else rl
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return {
            "month": m,
            "peso": peso,
            "ticket": tick,
            "peso_audit": pa,
            "ticket_audit": ta,
            "updated_by": ub,
            "updated_role": ur,
            "updated_at": ua,
        }

    def get_tracker_months(self, source: str, year: int):
        """Return tracker months (shared) with audit fields."""
        src = str(source or '').strip()
        y = int(year)
        if not src:
            return {}
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tm.month, tm.amount, tm.first_amount, tm.prev_amount, tm.created_by, tm.created_role, tm.created_at, "
            "tm.updated_by, tm.updated_role, tm.updated_at, tm.tickets_sold, tm.ticket_pad_id, "
            "tm.revenue_entry_mode, tp.book_number "
            "FROM tracker_monthly tm "
            "LEFT JOIN ticket_pads tp ON tp.id = tm.ticket_pad_id "
            "WHERE tm.source = ? AND tm.year = ?",
            (src, y),
        )
        rows = cursor.fetchall()
        conn.close()
        out = {}
        for r in rows:
            m = int(r[0])
            rem = self._normalize_revenue_entry_mode(r[12]) if len(r) > 12 else None
            out[m] = {
                "month": m,
                "amount": float(r[1] or 0),
                "first_amount": (float(r[2]) if r[2] is not None else None),
                "prev_amount": (float(r[3]) if r[3] is not None else None),
                "created_by": r[4] or "user",
                "created_role": r[5] or "staff",
                "created_at": r[6],
                "updated_by": r[7],
                "updated_role": r[8],
                "updated_at": r[9],
                "tickets_sold": (int(r[10]) if r[10] is not None else None),
                "ticket_pad_id": (int(r[11]) if r[11] is not None else None),
                "revenue_entry_mode": rem,
                "pad_book_number": (r[13] if len(r) > 13 and r[13] is not None else None),
            }
        return out

    def get_ticket_pads_for_source(self, source: str):
        """List ticket pads for a category with remaining counts."""
        src = str(source or '').strip()
        if not src:
            return []
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tp.id, tp.book_number, tp.ticket_count, tp.pad_value_php, tp.tickets_consumed, "
            "(tp.ticket_count - tp.tickets_consumed) AS remaining, "
            "CASE WHEN EXISTS ("
            "  SELECT 1 FROM tracker_monthly tm "
            "  WHERE tm.ticket_pad_id = tp.id AND tm.source = tp.source"
            ") THEN 0 ELSE 1 END AS can_delete "
            "FROM ticket_pads tp WHERE tp.source = ? ORDER BY tp.id ASC",
            (src,),
        )
        rows = cursor.fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(
                {
                    "id": int(r[0]),
                    "book_number": str(r[1] or ""),
                    "ticket_count": int(r[2] or 0),
                    "pad_value_php": float(r[3] or 0),
                    "tickets_consumed": int(r[4] or 0),
                    "remaining": int(r[5] or 0),
                    "can_delete": bool(int(r[6] or 0)),
                }
            )
        return out

    def get_unassigned_ticket_pads(self):
        """List ticket pads in the global pool (not yet linked to an income category)."""
        pool = self.TICKET_PAD_POOL_SOURCE
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tp.id, tp.book_number, tp.ticket_count, tp.pad_value_php, tp.tickets_consumed, "
            "(tp.ticket_count - tp.tickets_consumed) AS remaining, "
            "CASE WHEN EXISTS (SELECT 1 FROM tracker_monthly tm WHERE tm.ticket_pad_id = tp.id) "
            "THEN 0 ELSE 1 END AS can_delete "
            "FROM ticket_pads tp WHERE tp.source = ? ORDER BY tp.id ASC",
            (pool,),
        )
        rows = cursor.fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(
                {
                    "id": int(r[0]),
                    "book_number": str(r[1] or ""),
                    "ticket_count": int(r[2] or 0),
                    "pad_value_php": float(r[3] or 0),
                    "tickets_consumed": int(r[4] or 0),
                    "remaining": int(r[5] or 0),
                    "can_delete": bool(int(r[6] or 0)),
                }
            )
        return out

    def get_ticket_pad_stub_global_info(self, book_number: str):
        """
        If any ticket_pads row uses this stub/book number (trimmed match), return its source and pool flag.
        Used to block duplicate waiting-list vs category registrations.
        """
        bn = str(book_number or "").strip()
        if not bn:
            return None
        pool = self.TICKET_PAD_POOL_SOURCE
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, source FROM ticket_pads WHERE TRIM(book_number) = TRIM(?) ORDER BY id ASC LIMIT 1",
                (bn,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            sid = str(row[1] or "").strip()
            return {"id": int(row[0]), "source": sid, "in_pool": sid == pool}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def add_unassigned_ticket_pad(self, book_number: str, ticket_count: int):
        """Register a stub in the pool before it is assigned to an income category."""
        pool = self.TICKET_PAD_POOL_SOURCE
        bn = str(book_number or "").strip()
        tc = int(ticket_count)
        if not bn:
            raise ValueError("Ticket book / stub number is required")
        if tc <= 0:
            raise ValueError("Tickets in this pad must be greater than zero")
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM ticket_pads WHERE TRIM(book_number) = TRIM(?) AND source != ? LIMIT 1",
            (bn, pool),
        )
        if cursor.fetchone():
            conn.close()
            raise ValueError(
                "That stub/book number is already registered on an income category. "
                "Use a different number or edit the existing pad in that category."
            )
        try:
            cursor.execute(
                "INSERT INTO ticket_pads (source, book_number, ticket_count, pad_value_php, tickets_consumed) "
                "VALUES (?, ?, ?, ?, 0)",
                (pool, bn, tc, 0.0),
            )
            pid = int(cursor.lastrowid)
            conn.commit()
            return {
                "id": pid,
                "source": pool,
                "book_number": bn,
                "ticket_count": tc,
                "pad_value_php": 0.0,
                "tickets_consumed": 0,
                "remaining": tc,
                "can_delete": True,
            }
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise ValueError("That stub number is already in the unassigned list.")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def assign_ticket_pad_from_pool(self, pad_id: int, target_source: str):
        """Move a pad from the pool into a real income category; recomputes pad value from category ticket price."""
        pool = self.TICKET_PAD_POOL_SOURCE
        tgt = str(target_source or "").strip()
        if not tgt or tgt == pool:
            raise ValueError("Choose a valid income category.")
        pid = int(pad_id)
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT book_number, ticket_count, tickets_consumed FROM ticket_pads WHERE id = ? AND source = ?",
                (pid, pool),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Unassigned stub not found.")
            bn = str(row[0] or "").strip()
            tc = int(row[1] or 0)
            cons = int(row[2] or 0)
            if cons > 0:
                raise ValueError("This stub already has ticket usage and cannot be reassigned.")
            if not bn or tc <= 0:
                raise ValueError("Invalid stub record.")

            cursor.execute(
                "SELECT COALESCE(ticket_unit_php, 0) FROM categories WHERE id = ? AND is_active = 1 LIMIT 1",
                (tgt,),
            )
            urow = cursor.fetchone()
            unit = float(urow[0] or 0) if urow else 0.0
            if unit <= 0:
                raise ValueError("Set ticket price (₱ per ticket) for this category before assigning a stub.")

            cursor.execute(
                "SELECT 1 FROM ticket_pads WHERE source = ? AND book_number = ? LIMIT 1",
                (tgt, bn),
            )
            if cursor.fetchone():
                raise ValueError("That stub number already exists for this category.")

            pad_value = round(float(tc) * unit, 2)
            cursor.execute(
                "UPDATE ticket_pads SET source = ?, pad_value_php = ? WHERE id = ? AND source = ?",
                (tgt, pad_value, pid, pool),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise ValueError("Could not assign stub (it may have been removed).")
            conn.commit()
            cursor.execute(
                "SELECT id, book_number, ticket_count, pad_value_php, tickets_consumed, "
                "(ticket_count - tickets_consumed) FROM ticket_pads WHERE id = ? AND source = ?",
                (pid, tgt),
            )
            r2 = cursor.fetchone()
            if not r2:
                return None
            return {
                "id": int(r2[0]),
                "book_number": str(r2[1] or ""),
                "ticket_count": int(r2[2] or 0),
                "pad_value_php": float(r2[3] or 0),
                "tickets_consumed": int(r2[4] or 0),
                "remaining": int(r2[5] or 0),
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def add_ticket_pad(self, source: str, book_number: str, ticket_count: int):
        """Register a new numbered ticket pad for a category."""
        src = str(source or '').strip()
        bn = str(book_number or '').strip()
        tc = int(ticket_count)
        if not src:
            raise ValueError("source is required")
        if not bn:
            raise ValueError("Ticket book / stub number is required")
        if tc <= 0:
            raise ValueError("Tickets in this pad must be greater than zero")

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(ticket_unit_php, 0) FROM categories WHERE id = ? AND is_active = 1 LIMIT 1",
            (src,),
        )
        urow = cursor.fetchone()
        unit = float(urow[0] or 0) if urow else 0.0
        if unit <= 0:
            conn.close()
            raise ValueError("Set ticket price (₱ per ticket) for this category before registering a pad.")
        pad_value = round(float(tc) * unit, 2)

        pool = self.TICKET_PAD_POOL_SOURCE
        cursor.execute(
            "SELECT 1 FROM ticket_pads WHERE TRIM(book_number) = TRIM(?) AND source = ? LIMIT 1",
            (bn, pool),
        )
        if cursor.fetchone():
            conn.close()
            raise ValueError(
                "That stub/book number is already on the waiting list. Pick it under Book / stub to assign "
                "to this category, or use a different number."
            )

        try:
            cursor.execute(
                "INSERT INTO ticket_pads (source, book_number, ticket_count, pad_value_php, tickets_consumed) "
                "VALUES (?, ?, ?, ?, 0)",
                (src, bn, tc, pad_value),
            )
            pid = int(cursor.lastrowid)
            conn.commit()
            return {
                "id": pid,
                "source": src,
                "book_number": bn,
                "ticket_count": tc,
                "pad_value_php": pad_value,
                "tickets_consumed": 0,
                "remaining": tc,
            }
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise ValueError("That ticket book / stub number already exists for this category.")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def update_ticket_pad(self, source: str, pad_id: int, book_number: str | None = None, ticket_count: int | None = None):
        """Update book/stub label and/or pad size. ticket_count cannot be below tickets already consumed."""
        src = str(source or '').strip()
        pid = int(pad_id)
        if not src:
            raise ValueError("source is required")
        if book_number is None and ticket_count is None:
            raise ValueError("Nothing to update")

        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT ticket_count, tickets_consumed, book_number FROM ticket_pads WHERE id = ? AND source = ?",
                (pid, src),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Ticket pad not found for this category.")
            cur_tc = int(row[0] or 0)
            cur_cons = int(row[1] or 0)
            cur_bn = str(row[2] or "")

            new_bn = cur_bn if book_number is None else str(book_number or "").strip()
            if book_number is not None and not new_bn:
                raise ValueError("Ticket book / stub number is required")

            new_tc = cur_tc if ticket_count is None else int(ticket_count)
            if ticket_count is not None and new_tc <= 0:
                raise ValueError("Tickets in this pad must be greater than zero")
            if new_tc < cur_cons:
                raise ValueError(
                    f"Tickets in this pad cannot be less than already used ({cur_cons})."
                )

            if book_number is not None and new_bn != cur_bn:
                cursor.execute(
                    "SELECT 1 FROM ticket_pads WHERE TRIM(book_number) = TRIM(?) AND id != ? LIMIT 1",
                    (new_bn, pid),
                )
                if cursor.fetchone():
                    raise ValueError(
                        "That stub/book number is already in use (waiting list or another category)."
                    )

            pool = self.TICKET_PAD_POOL_SOURCE
            if src == pool:
                if ticket_count is not None:
                    new_pval = 0.0
                    if book_number is not None:
                        cursor.execute(
                            "UPDATE ticket_pads SET book_number = ?, ticket_count = ?, pad_value_php = ? "
                            "WHERE id = ? AND source = ?",
                            (new_bn, new_tc, new_pval, pid, src),
                        )
                    else:
                        cursor.execute(
                            "UPDATE ticket_pads SET ticket_count = ?, pad_value_php = ? WHERE id = ? AND source = ?",
                            (new_tc, new_pval, pid, src),
                        )
                else:
                    cursor.execute(
                        "UPDATE ticket_pads SET book_number = ? WHERE id = ? AND source = ?",
                        (new_bn, pid, src),
                    )
            else:
                cursor.execute(
                    "SELECT COALESCE(ticket_unit_php, 0) FROM categories WHERE id = ? AND is_active = 1 LIMIT 1",
                    (src,),
                )
                urow = cursor.fetchone()
                unit = float(urow[0] or 0) if urow else 0.0
                if unit <= 0:
                    raise ValueError("Set ticket price (₱ per ticket) for this category before updating a pad.")

                if ticket_count is not None:
                    new_pval = round(float(new_tc) * unit, 2)
                    if book_number is not None:
                        cursor.execute(
                            "UPDATE ticket_pads SET book_number = ?, ticket_count = ?, pad_value_php = ? "
                            "WHERE id = ? AND source = ?",
                            (new_bn, new_tc, new_pval, pid, src),
                        )
                    else:
                        cursor.execute(
                            "UPDATE ticket_pads SET ticket_count = ?, pad_value_php = ? WHERE id = ? AND source = ?",
                            (new_tc, new_pval, pid, src),
                        )
                else:
                    cursor.execute(
                        "UPDATE ticket_pads SET book_number = ? WHERE id = ? AND source = ?",
                        (new_bn, pid, src),
                    )
            conn.commit()
            cursor.execute(
                "SELECT id, book_number, ticket_count, pad_value_php, tickets_consumed, "
                "(ticket_count - tickets_consumed) FROM ticket_pads WHERE id = ? AND source = ?",
                (pid, src),
            )
            r2 = cursor.fetchone()
            if not r2:
                return None
            return {
                "id": int(r2[0]),
                "book_number": str(r2[1] or ""),
                "ticket_count": int(r2[2] or 0),
                "pad_value_php": float(r2[3] or 0),
                "tickets_consumed": int(r2[4] or 0),
                "remaining": int(r2[5] or 0),
            }
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise ValueError("That ticket book / stub number already exists for this category.")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def delete_ticket_pad(self, source: str, pad_id: int):
        """Remove a pad only if no tracker month row references it."""
        src = str(source or '').strip()
        pid = int(pad_id)
        if not src:
            raise ValueError("source is required")
        conn = self._connect()
        cursor = conn.cursor()
        try:
            if src == self.TICKET_PAD_POOL_SOURCE:
                cursor.execute(
                    "SELECT COUNT(*) FROM tracker_monthly WHERE ticket_pad_id = ?",
                    (pid,),
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM tracker_monthly WHERE ticket_pad_id = ? AND source = ?",
                    (pid, src),
                )
            n = int(cursor.fetchone()[0] or 0)
            if n > 0:
                raise ValueError(
                    "This ticket book is linked to revenue entries and cannot be deleted."
                )
            cursor.execute("DELETE FROM ticket_pads WHERE id = ? AND source = ?", (pid, src))
            if cursor.rowcount == 0:
                conn.rollback()
                raise ValueError("Ticket pad not found for this category.")
            conn.commit()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _normalize_revenue_entry_mode(raw):
        """'monthly' = headline month total save; 'daily' = last save from day grid (per-day data may still exist)."""
        if raw is None:
            return None
        s = str(raw).strip().lower()
        return s if s in ("monthly", "daily") else None

    def upsert_tracker_month(
        self,
        source: str,
        year: int,
        month: int,
        amount: float,
        acting_username: str,
        acting_role: str,
        tickets_sold=None,
        ticket_pad_id=None,
        revenue_entry_mode=None,
    ):
        """Insert or update a tracker month entry; optionally ties ticket sales to a ticket pad."""
        src = str(source or '').strip()
        y = int(year)
        m = int(month)
        amt = float(amount)
        if not src:
            raise ValueError("source is required")
        if m < 0 or m > 11:
            raise ValueError("month must be 0-11")
        if amt <= 0:
            raise ValueError("amount must be > 0")
        by = str(acting_username or 'user').strip() or 'user'
        rl = str(acting_role or 'staff').strip().lower() or 'staff'

        new_ts = None if tickets_sold is None else int(tickets_sold)
        new_pid = None if ticket_pad_id is None else int(ticket_pad_id)
        if new_ts is not None and new_ts <= 0:
            raise ValueError("tickets_sold must be greater than zero")

        entry_mode = self._normalize_revenue_entry_mode(revenue_entry_mode)

        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT COALESCE(ticket_unit_php, 0) FROM categories WHERE id = ? AND is_active = 1 LIMIT 1",
                (src,),
            )
            urow = cursor.fetchone()
            unit = float(urow[0] or 0) if urow else 0.0

            cursor.execute(
                "SELECT id, amount, first_amount, tickets_sold, ticket_pad_id, revenue_entry_mode FROM tracker_monthly "
                "WHERE source = ? AND year = ? AND month = ?",
                (src, y, m),
            )
            row = cursor.fetchone()

            old_ts = int(row[3]) if row and row[3] is not None else None
            old_pid = int(row[4]) if row and row[4] is not None else None

            if old_ts is not None and old_pid is not None and old_ts > 0:
                cursor.execute(
                    "UPDATE ticket_pads SET tickets_consumed = tickets_consumed - ? WHERE id = ? AND source = ?",
                    (old_ts, old_pid, src),
                )

            if new_ts is not None:
                if unit <= 0:
                    raise ValueError("Set ticket price (₱ per ticket) for this category before entering by tickets.")
                # Whole-peso month total as entered. May be below tickets×unit when staff overrides the headline figure.
                amt_rounded = round(amt)
                amt = float(amt_rounded)
                if new_pid is None:
                    cursor.execute(
                        "SELECT id FROM ticket_pads WHERE source = ? AND tickets_consumed < ticket_count "
                        "ORDER BY id ASC LIMIT 1",
                        (src,),
                    )
                    prow = cursor.fetchone()
                    if not prow:
                        raise ValueError(
                            "No ticket pad has remaining tickets. Register a new ticket book / stub in Ticket pads."
                        )
                    new_pid = int(prow[0])
                cursor.execute(
                    "SELECT ticket_count, tickets_consumed FROM ticket_pads WHERE id = ? AND source = ?",
                    (new_pid, src),
                )
                pc = cursor.fetchone()
                if not pc:
                    raise ValueError("Invalid ticket pad for this category.")
                pad_remaining = int(pc[0] or 0) - int(pc[1] or 0)
                if new_ts > pad_remaining:
                    raise ValueError(f"This ticket pad only has {pad_remaining} ticket(s) left.")
                cursor.execute(
                    "UPDATE ticket_pads SET tickets_consumed = tickets_consumed + ? WHERE id = ? AND source = ?",
                    (new_ts, new_pid, src),
                )
            else:
                new_ts = None
                new_pid = None

            if not row:
                final_mode = entry_mode
                cursor.execute(
                    "INSERT INTO tracker_monthly (source, year, month, amount, first_amount, created_by, created_role, "
                    "tickets_sold, ticket_pad_id, revenue_entry_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (src, y, m, amt, amt, by, rl, new_ts, new_pid, final_mode),
                )
                conn.commit()
                return {
                    "month": m,
                    "amount": amt,
                    "first_amount": amt,
                    "prev_amount": None,
                    "tickets_sold": new_ts,
                    "ticket_pad_id": new_pid,
                    "revenue_entry_mode": final_mode,
                    "by": {"username": by, "role": rl},
                    "edited": False,
                }

            old_mode = row[5] if len(row) > 5 else None
            final_mode = entry_mode if entry_mode is not None else old_mode
            _id, prev_amt, first_amt = int(row[0]), float(row[1] or 0), row[2]
            if first_amt is None:
                first_amt = prev_amt
            cursor.execute(
                "UPDATE tracker_monthly SET amount = ?, prev_amount = ?, first_amount = ?, "
                "updated_by = ?, updated_role = ?, updated_at = CURRENT_TIMESTAMP, "
                "tickets_sold = ?, ticket_pad_id = ?, revenue_entry_mode = ? WHERE id = ?",
                (amt, prev_amt, float(first_amt or 0), by, rl, new_ts, new_pid, final_mode, _id),
            )
            conn.commit()
            return {
                "month": m,
                "amount": amt,
                "first_amount": float(first_amt or 0),
                "prev_amount": prev_amt,
                "tickets_sold": new_ts,
                "ticket_pad_id": new_pid,
                "revenue_entry_mode": final_mode,
                "by": {"username": by, "role": rl},
                "edited": True,
            }
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def delete_tracker_month(self, source: str, year: int, month: int):
        """Remove one shared tracker month row and its daily detail; undo ticket pad consumption when applicable."""
        src = str(source or "").strip()
        y = int(year)
        m = int(month)
        if not src:
            raise ValueError("source is required")
        if m < 0 or m > 11:
            raise ValueError("month must be 0-11")
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id, ticket_pad_id, tickets_sold FROM tracker_monthly "
                "WHERE source = ? AND year = ? AND month = ?",
                (src, y, m),
            )
            row = cursor.fetchone()
            if not row:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return False
            old_pid = row[1]
            old_ts = row[2]
            if old_pid is not None and old_ts is not None and int(old_ts or 0) > 0:
                try:
                    cursor.execute(
                        "UPDATE ticket_pads SET tickets_consumed = tickets_consumed - ? WHERE id = ? AND source = ?",
                        (int(old_ts), int(old_pid), src),
                    )
                except Exception:
                    pass
            cursor.execute(
                "DELETE FROM tracker_monthly WHERE source = ? AND year = ? AND month = ?",
                (src, y, m),
            )
            try:
                cursor.execute(
                    "DELETE FROM tracker_daily_detail WHERE source = ? AND year = ? AND month = ?",
                    (src, y, m),
                )
            except Exception:
                pass
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_all_tracker_monthly_totals(self):
        """
        Monthly totals per source per year from shared tracker_monthly table.
        Output:
          { source: { year_int: [12 floats] } }
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT source, year, month, amount FROM tracker_monthly"
        )
        rows = cursor.fetchall()
        conn.close()
        out = {}
        for src, y, m, amt in rows:
            try:
                source = str(src)
                year = int(y)
                month = int(m)
                if month < 0 or month > 11:
                    continue
                amount = float(amt or 0)
            except Exception:
                continue
            if source not in out:
                out[source] = {}
            if year not in out[source]:
                out[source][year] = [0.0] * 12
            out[source][year][month] = amount
        return out

    def reset_tracker_year(self, year: int, source: str | None = None):
        """Delete tracker_monthly entries for a given year (optionally per source)."""
        y = int(year)
        src = (str(source).strip() if source is not None else '')
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            if src:
                cursor.execute(
                    "SELECT ticket_pad_id, tickets_sold FROM tracker_monthly WHERE year = ? AND source = ? "
                    "AND ticket_pad_id IS NOT NULL AND COALESCE(tickets_sold, 0) > 0",
                    (y, src),
                )
            else:
                cursor.execute(
                    "SELECT ticket_pad_id, tickets_sold FROM tracker_monthly WHERE year = ? "
                    "AND ticket_pad_id IS NOT NULL AND COALESCE(tickets_sold, 0) > 0",
                    (y,),
                )
            for pid, ts in cursor.fetchall() or []:
                try:
                    cursor.execute(
                        "UPDATE ticket_pads SET tickets_consumed = tickets_consumed - ? WHERE id = ?",
                        (int(ts or 0), int(pid)),
                    )
                except Exception:
                    pass
            if src:
                cursor.execute("DELETE FROM tracker_monthly WHERE year = ? AND source = ?", (y, src))
                try:
                    cursor.execute(
                        "DELETE FROM tracker_daily_detail WHERE year = ? AND source = ?", (y, src)
                    )
                except Exception:
                    pass
            else:
                cursor.execute("DELETE FROM tracker_monthly WHERE year = ?", (y,))
                try:
                    cursor.execute("DELETE FROM tracker_daily_detail WHERE year = ?", (y,))
                except Exception:
                    pass
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True

    def authenticate_user(self, username: str, password: str):
        """Return user dict if valid active user (by username or registered email), else None."""
        u = str(username or '').strip()
        p = str(password or '')
        if not u or not p:
            return None
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, username, password_hash, role, is_active, COALESCE(session_version, 0) FROM users '
            'WHERE LOWER(username)=LOWER(?) OR LOWER(TRIM(email))=LOWER(TRIM(?)) LIMIT 1',
            (u, u),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        uid, uname, ph, role, is_active, sv = row
        if int(is_active or 0) != 1:
            return None
        try:
            ok = check_password_hash(ph, p)
        except Exception:
            ok = False
        if not ok:
            return None
        return {'id': int(uid), 'username': uname, 'role': role, 'session_version': int(sv or 0)}

    # ---------------------------
    # Email-based password reset
    # ---------------------------
    def _normalize_email(self, email: str) -> str:
        e = str(email or '').strip()
        # Keep case-insensitive comparison
        return e.lower()

    def _validate_email(self, email: str) -> str:
        e = str(email or '').strip()
        if not e:
            raise ValueError("Email is required")
        if len(e) > 254:
            raise ValueError("Email is too long")
        # Simple sanity check (not RFC-perfect, but avoids obvious bad values)
        if '@' not in e or e.startswith('@') or e.endswith('@'):
            raise ValueError("Invalid email address")
        return e

    def get_user_by_username_or_email(self, identifier: str):
        """
        Find a user by username OR email (case-insensitive).
        Returns dict with id/username/email/is_active or None.
        """
        ident = str(identifier or '').strip()
        if not ident:
            return None
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username, email, is_active FROM users "
            "WHERE LOWER(username)=LOWER(?) OR LOWER(TRIM(email))=LOWER(TRIM(?)) "
            "LIMIT 1",
            (ident, ident),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {"id": int(row[0]), "username": row[1], "email": row[2], "is_active": int(row[3] or 0)}

    def get_user_email(self, user_id: int) -> str | None:
        uid = int(user_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE id = ? LIMIT 1", (uid,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        em = row[0]
        if em is None:
            return None
        s = str(em).strip()
        return s or None

    def set_user_email(self, user_id: int, email: str | None):
        """
        Set or clear a user's email (used for email OTP reset).
        - email=None/'' clears the field
        - non-empty email is validated and stored
        """
        uid = int(user_id)
        raw = '' if email is None else str(email or '').strip()

        # Retry on transient locks (e.g., background initialization writes)
        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT id, is_active FROM users WHERE id = ? LIMIT 1", (uid,))
                row = cursor.fetchone()
                if not row:
                    conn.close()
                    raise ValueError("User not found")
                if not raw:
                    cursor.execute("UPDATE users SET email = NULL WHERE id = ?", (uid,))
                else:
                    em = self._validate_email(raw)
                    cursor.execute("UPDATE users SET email = ? WHERE id = ?", (em, uid))
                conn.commit()
                conn.close()
                return True
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise sqlite3.OperationalError("database is locked")

    def create_password_reset_otp(self, user_id: int, sent_to: str | None = None, ttl_minutes: int = 15, digits: int = 6) -> str:
        """
        Create a single-use numeric OTP and persist its hash.
        Returns the *raw* OTP (only returned once; send via email).
        """
        uid = int(user_id)
        ttl = int(ttl_minutes)
        if ttl < 3:
            ttl = 3
        if ttl > 60:
            ttl = 60
        d = int(digits)
        if d < 4:
            d = 4
        if d > 10:
            d = 10
        # 6-digit OTP by default (000000..999999)
        otp = str(secrets.randbelow(10 ** d)).zfill(d)
        th = hashlib.sha256(otp.encode('utf-8')).hexdigest()
        expires = datetime.utcnow() + timedelta(minutes=ttl)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO password_resets (user_id, token_hash, sent_to, expires_at) VALUES (?, ?, ?, ?)",
            (uid, th, (str(sent_to).strip() if sent_to is not None else None), expires.strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        conn.close()
        return otp

    def verify_password_reset_token(self, token: str) -> int:
        """
        Validate a reset OTP without consuming it.
        Returns user_id when valid; raises ValueError otherwise.
        """
        t = str(token or '').strip()
        if not t:
            raise ValueError("Reset code is required")

        th = hashlib.sha256(t.encode('utf-8')).hexdigest()
        now = datetime.utcnow()

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, expires_at, used_at FROM password_resets WHERE token_hash = ? ORDER BY id DESC LIMIT 1",
            (th,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("Invalid or expired reset code")
        _rid, uid, expires_at, used_at = row
        if used_at:
            conn.close()
            raise ValueError("This reset code was already used")
        try:
            exp = datetime.strptime(str(expires_at), '%Y-%m-%d %H:%M:%S')
        except Exception:
            exp = now - timedelta(seconds=1)
        if exp < now:
            conn.close()
            raise ValueError("Invalid or expired reset code")

        cursor.execute("SELECT is_active FROM users WHERE id = ? LIMIT 1", (int(uid),))
        ur = cursor.fetchone()
        if not ur or int(ur[0] or 0) != 1:
            conn.close()
            raise ValueError("Account is disabled. Please contact your administrator.")

        conn.close()
        return int(uid)

    def consume_password_reset_token(self, token: str, new_password: str) -> int:
        """
        Validate and consume a token to reset password.
        """
        t = str(token or '').strip()
        np = str(new_password or '').strip()
        if not t:
            raise ValueError("Reset code is required")
        if not np or len(np) < 4:
            raise ValueError("New password must be at least 4 characters")

        th = hashlib.sha256(t.encode('utf-8')).hexdigest()
        now = datetime.utcnow()

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, expires_at, used_at FROM password_resets WHERE token_hash = ? ORDER BY id DESC LIMIT 1",
            (th,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("Invalid or expired reset code")
        rid, uid, expires_at, used_at = row
        if used_at:
            conn.close()
            raise ValueError("This reset code was already used")
        try:
            exp = datetime.strptime(str(expires_at), '%Y-%m-%d %H:%M:%S')
        except Exception:
            exp = now - timedelta(seconds=1)
        if exp < now:
            conn.close()
            raise ValueError("Invalid or expired reset code")

        # Ensure the user is still active
        cursor.execute("SELECT is_active FROM users WHERE id = ? LIMIT 1", (int(uid),))
        ur = cursor.fetchone()
        if not ur or int(ur[0] or 0) != 1:
            conn.close()
            raise ValueError("Account is disabled. Please contact your administrator.")

        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(np), int(uid)))
        cursor.execute("UPDATE users SET session_version = COALESCE(session_version, 0) + 1 WHERE id = ?", (int(uid),))
        cursor.execute("UPDATE password_resets SET used_at = ? WHERE id = ?", (now.strftime('%Y-%m-%d %H:%M:%S'), int(rid)))
        conn.commit()
        conn.close()
        return int(uid)

    def get_user_active_flag(self, username: str):
        """
        Return account active status for a username or email (case-insensitive).
        - True/False if user exists
        - None if user not found
        """
        u = str(username or '').strip()
        if not u:
            return None
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT is_active FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(TRIM(email))=LOWER(TRIM(?)) LIMIT 1',
            (u, u),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return int(row[0] or 0) == 1

    def get_user_session_version(self, user_id: int) -> int:
        uid = int(user_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(session_version, 0) FROM users WHERE id = ? LIMIT 1", (uid,))
        row = cursor.fetchone()
        conn.close()
        return int(row[0] or 0) if row else 0

    def list_roles(self, include_inactive: bool = False):
        conn = self._connect()
        cursor = conn.cursor()
        if include_inactive:
            cursor.execute("SELECT id, name, is_admin, is_active, created_at FROM roles ORDER BY LOWER(name) ASC")
        else:
            cursor.execute("SELECT id, name, is_admin, is_active, created_at FROM roles WHERE is_active = 1 ORDER BY LOWER(name) ASC")
        rows = cursor.fetchall()
        conn.close()
        return [
            {"id": int(r[0]), "name": r[1], "is_admin": int(r[2] or 0), "is_active": int(r[3] or 0), "created_at": r[4]}
            for r in rows
        ]

    def is_role_admin(self, role_name: str):
        rn = str(role_name or '').strip()
        if not rn:
            return False
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT is_admin, is_active FROM roles WHERE LOWER(name)=LOWER(?) LIMIT 1", (rn,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return rn.lower() == 'admin'
        return int(row[0] or 0) == 1 and int(row[1] or 0) == 1

    def _role_in_use_count(self, role_name: str):
        rn = str(role_name or '').strip()
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE LOWER(role)=LOWER(?)", (rn,))
        n = int(cursor.fetchone()[0] or 0)
        conn.close()
        return n

    def _active_admin_user_count(self):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1 AND LOWER(role) = 'admin'")
        n = int(cursor.fetchone()[0] or 0)
        conn.close()
        return n

    def create_role(self, name: str, is_admin: bool = False, is_active: bool = True):
        nm = str(name or '').strip().lower()
        if not nm:
            raise ValueError("Role name is required")
        if len(nm) > 40:
            raise ValueError("Role name is too long")
        if nm in ('admin', 'staff'):
            # base roles always exist; allow but no-op
            return True
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO roles (name, is_admin, is_active) VALUES (?, ?, ?)", (nm, 1 if is_admin else 0, 1 if is_active else 0))
        conn.commit()
        conn.close()
        return True

    def update_role(self, role_id: int, name: str | None = None, is_admin: bool | None = None):
        rid = int(role_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, is_admin, is_active FROM roles WHERE id = ?", (rid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("Role not found")
        old_name = str(row[1] or '')
        if old_name.lower() in ('admin', 'staff'):
            conn.close()
            raise ValueError("Base roles cannot be renamed/edited.")

        if name is not None:
            nm = str(name or '').strip().lower()
            if not nm:
                conn.close()
                raise ValueError("Role name is required")
            cursor.execute("UPDATE roles SET name = ? WHERE id = ?", (nm, rid))
            # Update users.role strings too
            cursor.execute("UPDATE users SET role = ? WHERE LOWER(role)=LOWER(?)", (nm, old_name))
        if is_admin is not None:
            cursor.execute("UPDATE roles SET is_admin = ? WHERE id = ?", (1 if bool(is_admin) else 0, rid))

        conn.commit()
        conn.close()
        return True

    def set_role_active(self, role_id: int, is_active: bool):
        rid = int(role_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, is_admin, is_active FROM roles WHERE id = ?", (rid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("Role not found")
        name = str(row[1] or '')
        is_admin = int(row[2] or 0) == 1
        cur_active = int(row[3] or 0)
        next_active = 1 if bool(is_active) else 0

        if name.lower() in ('admin', 'staff'):
            conn.close()
            raise ValueError("Base roles cannot be disabled.")

        if cur_active == 1 and next_active == 0 and is_admin:
            # Avoid disabling the last admin role if it would remove admin access
            if self._active_admin_user_count() <= 1:
                conn.close()
                raise ValueError("Cannot disable the last active admin.")

        # Allow disabling even if assigned to users. Users retain role string;
        # admin UI should indicate the role is disabled.

        cursor.execute("UPDATE roles SET is_active = ? WHERE id = ?", (next_active, rid))
        conn.commit()
        conn.close()
        return True

    def delete_role(self, role_id: int):
        rid = int(role_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM roles WHERE id = ?", (rid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("Role not found")
        name = str(row[1] or '')
        if name.lower() in ('admin', 'staff'):
            conn.close()
            raise ValueError("Base roles cannot be deleted.")
        if self._role_in_use_count(name) > 0:
            conn.close()
            raise ValueError("Cannot delete a role that is assigned to users.")
        cursor.execute("DELETE FROM roles WHERE id = ?", (rid,))
        conn.commit()
        conn.close()
        return True

    def touch_last_login(self, user_id: int):
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (int(user_id),))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def list_users(self):
        """List users for admin UI (no password hash)."""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                u.id,
                u.username,
                u.email,
                u.role,
                u.is_active,
                u.created_at,
                u.last_login,
                COALESCE(r.is_active, 1) AS role_is_active
            FROM users u
            LEFT JOIN roles r ON LOWER(r.name) = LOWER(u.role)
            ORDER BY LOWER(u.username) ASC
        """)
        rows = cursor.fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(
                {
                    "id": int(r[0]),
                    "username": r[1],
                    "email": r[2],
                    "role": r[3],
                    "is_active": int(r[4] or 0),
                    "created_at": r[5],
                    "last_login": r[6],
                    "role_is_active": int(r[7] or 0),
                }
            )
        return out

    def _active_admin_count(self):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1 AND LOWER(role) = 'admin'")
        n = int(cursor.fetchone()[0] or 0)
        conn.close()
        return n

    def create_user(self, username: str, password: str, role: str = "staff", is_active: bool = True, email: str | None = None):
        u = str(username or "").strip()
        p = str(password or "").strip()
        rl = (str(role or "staff").strip().lower()) or "staff"
        if not u:
            raise ValueError("Username is required")
        if not p or len(p) < 4:
            raise ValueError("Password must be at least 4 characters")
        em = None
        if email is not None:
            s = str(email or '').strip()
            if s:
                em = self._validate_email(s)
        # Role must exist and be active (except base roles)
        try:
            roles = self.list_roles(include_inactive=True)
            exists = any(str(r.get('name','')).lower() == rl for r in roles)
            active = any(str(r.get('name','')).lower() == rl and int(r.get('is_active') or 0) == 1 for r in roles)
            if not exists:
                raise ValueError("Role not found")
            if not active:
                raise ValueError("Role is disabled")
        except ValueError:
            raise
        except Exception:
            # fallback: only allow staff/admin
            if rl not in ("admin", "staff"):
                raise ValueError("Role not found")
        ph = generate_password_hash(p)

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, email, password_hash, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (u, em, ph, rl, 1 if bool(is_active) else 0),
        )
        conn.commit()
        uid = int(cursor.lastrowid)
        conn.close()
        return uid

    def update_user(self, user_id: int, role: str | None = None, email: str | None = None):
        uid = int(user_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, role, is_active, email FROM users WHERE id = ?", (uid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found")

        if email is not None:
            raw = str(email or '').strip()
            # allow clearing email by passing empty string
            if not raw:
                cursor.execute("UPDATE users SET email = NULL WHERE id = ?", (uid,))
            else:
                em = self._validate_email(raw)
                cursor.execute("UPDATE users SET email = ? WHERE id = ?", (em, uid))

        if role is not None:
            rl = str(role or "").strip().lower()
            # Validate role exists and active
            try:
                roles = self.list_roles(include_inactive=True)
                exists = any(str(r.get('name','')).lower() == rl for r in roles)
                active = any(str(r.get('name','')).lower() == rl and int(r.get('is_active') or 0) == 1 for r in roles)
                if not exists:
                    conn.close()
                    raise ValueError("Role not found")
                if not active:
                    conn.close()
                    raise ValueError("Role is disabled")
            except ValueError:
                raise
            cursor.execute("UPDATE users SET role = ? WHERE id = ?", (rl, uid))
            # Invalidate sessions so the user must log in again with new permissions
            cursor.execute("UPDATE users SET session_version = COALESCE(session_version, 0) + 1 WHERE id = ?", (uid,))

        conn.commit()
        conn.close()
        return True

    def set_user_active(self, user_id: int, is_active: bool, acting_user_id: int | None = None):
        uid = int(user_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, role, is_active FROM users WHERE id = ?", (uid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found")

        role = (row[1] or "").lower()
        currently_active = int(row[2] or 0)
        next_active = 1 if bool(is_active) else 0

        if acting_user_id is not None and int(acting_user_id) == uid and next_active == 0:
            conn.close()
            raise ValueError("You cannot disable your own account.")

        # Prevent disabling the last active admin
        if currently_active == 1 and next_active == 0 and role == "admin":
            if self._active_admin_count() <= 1:
                conn.close()
                raise ValueError("Cannot disable the last active admin.")

        cursor.execute("UPDATE users SET is_active = ? WHERE id = ?", (next_active, uid))
        # Invalidate sessions when account status changes
        cursor.execute("UPDATE users SET session_version = COALESCE(session_version, 0) + 1 WHERE id = ?", (uid,))
        conn.commit()
        conn.close()
        return True

    def reset_user_password(self, user_id: int, new_password: str, acting_user_id: int | None = None):
        uid = int(user_id)
        p = str(new_password or "").strip()
        if not p or len(p) < 4:
            raise ValueError("Password must be at least 4 characters")

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, role, is_active FROM users WHERE id = ?", (uid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found")
        if int(row[2] or 0) != 1:
            conn.close()
            raise ValueError("User is disabled.")

        ph = generate_password_hash(p)
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (ph, uid))
        conn.commit()
        conn.close()
        return True

    def recovery_set_password_and_enable(self, username: str, new_password: str):
        """
        Local / lockout recovery: set a new password, enable the account, and bump session_version.
        Use from a trusted maintenance script only (not exposed via HTTP).
        """
        u = str(username or '').strip()
        p = str(new_password or '').strip()
        if not u:
            raise ValueError("username is required")
        if not p or len(p) < 4:
            raise ValueError("Password must be at least 4 characters")
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1", (u,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found: " + u)
        uid = int(row[0])
        ph = generate_password_hash(p)
        cursor.execute(
            "UPDATE users SET password_hash = ?, is_active = 1, "
            "session_version = COALESCE(session_version, 0) + 1 WHERE id = ?",
            (ph, uid),
        )
        conn.commit()
        conn.close()
        return True

    def delete_user(self, user_id: int, acting_user_id: int | None = None):
        """
        Permanently delete a user account.
        Safety rules:
        - Cannot delete your own account
        - Cannot delete the last active admin
        """
        uid = int(user_id)
        if acting_user_id is not None and int(acting_user_id) == uid:
            raise ValueError("You cannot delete your own account.")

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, role, is_active FROM users WHERE id = ?", (uid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found")

        role = (row[1] or "").lower()
        is_active = int(row[2] or 0)
        if role == "admin" and is_active == 1:
            if self._active_admin_count() <= 1:
                conn.close()
                raise ValueError("Cannot delete the last active admin.")

        cursor.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()
        conn.close()
        return True

    def change_my_password(self, user_id: int, old_password: str, new_password: str):
        """Change password for the logged-in user after validating old password."""
        uid = int(user_id)
        oldp = str(old_password or '')
        newp = str(new_password or '').strip()
        if not newp or len(newp) < 4:
            raise ValueError("New password must be at least 4 characters")

        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash, is_active FROM users WHERE id = ?", (uid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError("User not found")
        if int(row[2] or 0) != 1:
            conn.close()
            raise ValueError("User is disabled")
        ph = row[1]
        try:
            ok = check_password_hash(ph, oldp)
        except Exception:
            ok = False
        if not ok:
            conn.close()
            raise ValueError("Old password is incorrect")

        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(newp), uid))
        conn.commit()
        conn.close()
        return True

    def init_default_categories(self, cursor):
        """Insert default income categories if categories table is empty."""
        try:
            cursor.execute('SELECT COUNT(*) FROM categories')
            if cursor.fetchone()[0] != 0:
                return
            defaults = [
                ('BUS-1', 'Bus Ticket 1', '🚌'),
                ('BUS-2', 'Bus Ticket 2', '🚍'),
                ('DELIVERY TRUCK', 'Delivery Truck', '🚛'),
                ('MOTORIZED VEHICLE', 'Motorized Tricycle', '🛺'),
                ('TOILET-LAVATORY', 'Toilet/Lavatory', '🚽'),
                ('STREET FOODS', 'Street Foods', '🍜'),
                ('LINER-MARKET', 'Market Liner', '🏪'),
                ('TABO', 'Tabo', '💧'),
                ('MARKET-RENTAL STALL-SPACE', 'Stall Rental', '🏬'),
                ('MARKET ELECTRIC', 'Market Electric', '⚡'),
            ]
            cursor.executemany(
                'INSERT INTO categories (id, name, icon, is_active) VALUES (?, ?, ?, 1)',
                defaults
            )
            print("[OK] Default categories initialized")
        except Exception as e:
            print(f"[WARN] Could not init default categories: {e}")

    def get_categories(self, active_only=True):
        """Return list of income categories."""
        conn = self._connect()
        cursor = conn.cursor()
        if active_only:
            cursor.execute(
                'SELECT id, name, icon, COALESCE(ticket_unit_php, 0) FROM categories WHERE is_active = 1 ORDER BY name ASC'
            )
        else:
            # Exclude removed categories (is_active = -1)
            cursor.execute(
                'SELECT id, name, icon, is_active, COALESCE(ticket_unit_php, 0) FROM categories WHERE is_active <> -1 ORDER BY name ASC'
            )
        rows = cursor.fetchall()
        conn.close()
        out = []
        for r in rows:
            if active_only:
                item = {
                    'id': r[0],
                    'name': r[1],
                    'icon': r[2] or '',
                    'ticket_unit_php': float(r[3] or 0),
                }
            else:
                item = {
                    'id': r[0],
                    'name': r[1],
                    'icon': r[2] or '',
                    'is_active': int(r[3] or 0),
                    'ticket_unit_php': float(r[4] or 0),
                }
            out.append(item)
        return out

    def update_category(self, category_id, name=None, icon=None, ticket_unit_php=_NO_TICKET_UNIT_UPDATE):
        """
        Update an existing category's name and/or icon and/or ticket unit price.
        Enforces unique active name (case-insensitive) excluding this category.
        """
        cid = str(category_id or '').strip()
        if not cid:
            raise ValueError("Category id is required")

        nm = None if name is None else str(name).strip()
        ic = None if icon is None else str(icon).strip()
        if nm is not None and not nm:
            raise ValueError("Category name is required")

        new_ticket = ticket_unit_php
        if new_ticket is not _NO_TICKET_UNIT_UPDATE:
            try:
                new_ticket = float(new_ticket)
            except (TypeError, ValueError):
                raise ValueError("Ticket value must be a number")
            if new_ticket < 0:
                raise ValueError("Ticket value cannot be negative")

        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    'SELECT id, name, icon, is_active, COALESCE(ticket_unit_php, 0) FROM categories WHERE id = ?',
                    (cid,),
                )
                row = cursor.fetchone()
                if row is None:
                    conn.close()
                    raise ValueError("Category not found")

                current_active = int(row[3] or 0)
                new_name = nm if nm is not None else (row[1] or '')
                new_icon = ic if ic is not None else (row[2] or '')
                ticket_use = float(row[4] or 0) if new_ticket is _NO_TICKET_UNIT_UPDATE else float(new_ticket)

                # Unique name check only among active categories (and only relevant if this category is active)
                if current_active == 1:
                    cursor.execute(
                        'SELECT id FROM categories WHERE is_active = 1 AND LOWER(TRIM(name)) = LOWER(TRIM(?)) AND id <> ?',
                        (new_name, cid)
                    )
                    if cursor.fetchone() is not None:
                        conn.close()
                        raise ValueError("Duplicate category name. Please choose a different name.")

                if new_ticket is _NO_TICKET_UNIT_UPDATE:
                    cursor.execute('UPDATE categories SET name = ?, icon = ? WHERE id = ?', (new_name, new_icon, cid))
                else:
                    cursor.execute(
                        'UPDATE categories SET name = ?, icon = ?, ticket_unit_php = ? WHERE id = ?',
                        (new_name, new_icon, ticket_use, cid),
                    )
                conn.commit()
                conn.close()
                return {
                    'id': cid,
                    'name': new_name,
                    'icon': new_icon,
                    'is_active': current_active,
                    'ticket_unit_php': ticket_use,
                }
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise ValueError("Database is busy. Please try again.")

    def set_category_active(self, category_id, is_active: bool):
        """Soft-disable or re-enable a category."""
        cid = str(category_id or '').strip()
        if not cid:
            raise ValueError("Category id is required")
        active_val = 1 if bool(is_active) else 0

        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute('SELECT id, name, icon, is_active FROM categories WHERE id = ?', (cid,))
                row = cursor.fetchone()
                if row is None:
                    conn.close()
                    raise ValueError("Category not found")

                nm = row[1] or ''
                ic = row[2] or ''

                if active_val == 1:
                    # When enabling, enforce unique active name
                    cursor.execute(
                        'SELECT id FROM categories WHERE is_active = 1 AND LOWER(TRIM(name)) = LOWER(TRIM(?)) AND id <> ?',
                        (nm, cid)
                    )
                    if cursor.fetchone() is not None:
                        conn.close()
                        raise ValueError("Duplicate category name. Please choose a different name.")

                cursor.execute('UPDATE categories SET is_active = ? WHERE id = ?', (active_val, cid))
                conn.commit()
                conn.close()
                return {'id': cid, 'name': nm, 'icon': ic, 'is_active': active_val}
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise ValueError("Database is busy. Please try again.")

    def remove_category(self, category_id):
        """
        Remove a category from UI lists without deleting historical records.
        We implement this as a soft-delete state: is_active = -1.
          - Active (shown): is_active = 1
          - Disabled (can re-enable): is_active = 0
          - Removed (hidden, not re-enable via UI): is_active = -1
        """
        cid = str(category_id or '').strip()
        if not cid:
            raise ValueError("Category id is required")

        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute('SELECT id, name, icon, is_active FROM categories WHERE id = ?', (cid,))
                row = cursor.fetchone()
                if row is None:
                    conn.close()
                    raise ValueError("Category not found")

                cursor.execute('UPDATE categories SET is_active = -1 WHERE id = ?', (cid,))
                conn.commit()
                conn.close()
                return {'id': cid, 'name': row[1] or '', 'icon': row[2] or '', 'is_active': -1}
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise ValueError("Database is busy. Please try again.")

    def add_category(self, category_id, name, icon=''):
        """Add a new income category. Raises ValueError on invalid/duplicate IDs."""
        cid = str(category_id or '').strip()
        nm = str(name or '').strip()
        ic = str(icon or '').strip()
        if not cid or not nm:
            raise ValueError("Category id and name are required")
        if len(cid) > 60:
            raise ValueError("Category id is too long")

        # Retry on transient locks (e.g., during background initialization writes)
        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                # Enforce unique name (case-insensitive) among active categories
                cursor.execute('SELECT id FROM categories WHERE is_active = 1 AND LOWER(TRIM(name)) = LOWER(TRIM(?))', (nm,))
                if cursor.fetchone() is not None:
                    conn.close()
                    raise ValueError("Category name already exists")
                # Ensure unique id
                cursor.execute('SELECT id FROM categories WHERE id = ?', (cid,))
                if cursor.fetchone() is not None:
                    conn.close()
                    raise ValueError("Category id already exists")

                # If icon not provided, choose a default icon deterministically
                ic_use = ic
                if not ic_use:
                    try:
                        cursor.execute('SELECT COUNT(*) FROM categories')
                        n = int(cursor.fetchone()[0] or 0)
                    except Exception:
                        n = 0
                    ic_use = self.DEFAULT_CATEGORY_ICONS[n % len(self.DEFAULT_CATEGORY_ICONS)]

                try:
                    cursor.execute(
                        'INSERT INTO categories (id, name, icon, is_active) VALUES (?, ?, ?, 1)',
                        (cid, nm, ic_use)
                    )
                except sqlite3.IntegrityError:
                    conn.close()
                    raise ValueError("Category name already exists")

                conn.commit()
                conn.close()
                return {'id': cid, 'name': nm, 'icon': ic_use}
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise ValueError("Database is busy. Please try again.")

    def _slug_category_id(self, name: str) -> str:
        """Create a stable, DB-safe category id from a name."""
        base = str(name or '').strip().upper()
        base = re.sub(r'\s+', ' ', base)
        base = re.sub(r'[^A-Z0-9 _-]+', '', base)
        base = base.replace(' ', '-')
        base = re.sub(r'-{2,}', '-', base).strip('-')
        if not base:
            base = 'NEW-CATEGORY'
        return base[:60]

    def add_category_auto(self, name, icon=''):
        """
        Add a new income category with an auto-generated unique ID.
        If the slug already exists, appends -2, -3, etc.
        """
        nm = str(name or '').strip()
        ic = str(icon or '').strip()
        if not nm:
            raise ValueError("Category name is required")

        # Retry on transient locks (e.g., during background initialization writes)
        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                # Enforce unique name (case-insensitive) among active categories
                cursor.execute(
                    'SELECT id FROM categories WHERE is_active = 1 AND LOWER(TRIM(name)) = LOWER(TRIM(?))',
                    (nm,)
                )
                if cursor.fetchone() is not None:
                    conn.close()
                    raise ValueError("Duplicate category name. Please choose a different name.")

                base = self._slug_category_id(nm)
                cid = base
                # Find a unique id
                k = 2
                while True:
                    cursor.execute('SELECT id FROM categories WHERE id = ?', (cid,))
                    if cursor.fetchone() is None:
                        break
                    suffix = f"-{k}"
                    cid = (base[: max(1, 60 - len(suffix))] + suffix)
                    k += 1

                # If icon not provided, auto-pick the next default icon
                ic_use = ic
                if not ic_use:
                    try:
                        cursor.execute('SELECT COUNT(*) FROM categories')
                        n = int(cursor.fetchone()[0] or 0)
                    except Exception:
                        n = 0
                    ic_use = self.DEFAULT_CATEGORY_ICONS[n % len(self.DEFAULT_CATEGORY_ICONS)]

                try:
                    cursor.execute(
                        'INSERT INTO categories (id, name, icon, is_active) VALUES (?, ?, ?, 1)',
                        (cid, nm, ic_use)
                    )
                except sqlite3.IntegrityError:
                    conn.close()
                    raise ValueError("Category name already exists")

                conn.commit()
                conn.close()
                return {'id': cid, 'name': nm, 'icon': ic_use}
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise ValueError("Database is busy. Please try again.")
    
    def save_historical_data(self, data):
        """Save historical data from all sources"""
        # Retry to avoid transient locks during background initialization / UI activity
        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute('DELETE FROM historical_data')

                for _, row in data.iterrows():
                    date_val = row['date']
                    if hasattr(date_val, 'month'):
                        month = date_val.month
                        year = date_val.year
                    else:
                        try:
                            parsed_date = datetime.strptime(str(date_val), '%Y-%m-%d')
                            month = parsed_date.month
                            year = parsed_date.year
                        except Exception:
                            month = 1
                            year = datetime.now().year

                    cursor.execute('''
                        INSERT INTO historical_data 
                        (date, amount_remitted, source, section, year, month)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val),
                        float(row['amount_remitted']),
                        row['source'],
                        row.get('section', ''),
                        row.get('year', year),
                        row.get('month', month)
                    ))

                conn.commit()
                conn.close()
                print(f"Saved {len(data)} historical records to database")
                return
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise sqlite3.OperationalError("database is locked")
    
    def save_user_income(self, source, monthly_income, income_date=None, notes='', created_by='user'):
        """Save user's monthly income input"""
        if income_date is None:
            income_date = datetime.now().date()

        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO user_income_inputs 
                    (income_date, source, monthly_income, notes, created_by)
                    VALUES (?, ?, ?, ?, ?)
                ''', (income_date, source, monthly_income, notes, created_by))
                conn.commit()
                last_id = cursor.lastrowid
                conn.close()
                return last_id
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise sqlite3.OperationalError("database is locked")
    
    def delete_user_income(self, entry_id):
        """Delete a specific user income entry by ID"""
        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute('DELETE FROM user_income_inputs WHERE id = ?', (entry_id,))
                conn.commit()
                rows_affected = cursor.rowcount
                conn.close()
                return rows_affected > 0
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise sqlite3.OperationalError("database is locked")
    
    def save_prediction(self, source, input_income, predicted_monthly, predicted_yearly, confidence):
        """Save prediction results"""
        for attempt in range(20):
            conn = self._connect()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO predictions 
                    (source, input_income, predicted_monthly, predicted_yearly, confidence_score)
                    VALUES (?, ?, ?, ?, ?)
                ''', (source, input_income, predicted_monthly, predicted_yearly, confidence))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as oe:
                try:
                    conn.close()
                except Exception:
                    pass
                if 'locked' in str(oe).lower():
                    time.sleep(0.25)
                    continue
                raise

        raise sqlite3.OperationalError("database is locked")
    
    def get_user_inputs(self, source=None, months=None, limit=None):
        """Get user inputs with optional filters"""
        conn = sqlite3.connect(self.db_name)
        
        query = "SELECT id, income_date, source, monthly_income, notes, input_date FROM user_income_inputs"
        params = []
        conditions = []
        
        if source:
            conditions.append("source = ?")
            params.append(source)
        
        if months:
            conditions.append("income_date >= date('now', '-{} months')".format(months))
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY income_date DESC, id DESC"
        
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        
        df = pd.read_sql_query(query, conn, params=params if params else None)
        conn.close()
        return df
    
    def get_predictions_history(self, source=None, limit=20):
        """Get prediction history"""
        conn = sqlite3.connect(self.db_name)
        
        if source:
            query = '''
                SELECT * FROM predictions 
                WHERE source = ? 
                ORDER BY prediction_date DESC LIMIT ?
            '''
            df = pd.read_sql_query(query, conn, params=(source, limit))
        else:
            query = '''
                SELECT * FROM predictions 
                ORDER BY prediction_date DESC LIMIT ?
            '''
            df = pd.read_sql_query(query, conn, params=(limit,))
        
        conn.close()
        return df
    
    def generate_report(self, source, start_date, end_date):
        """Generate comprehensive report for a date range"""
        conn = sqlite3.connect(self.db_name)
        
        query = '''
            SELECT 
                income_date,
                monthly_income,
                notes
            FROM user_income_inputs
            WHERE source = ? 
                AND income_date BETWEEN ? AND ?
            ORDER BY income_date
        '''
        
        df = pd.read_sql_query(query, conn, params=(source, start_date, end_date))
        
        if not df.empty:
            total_income = df['monthly_income'].sum()
            avg_monthly = df['monthly_income'].mean()
            months_count = len(df)
            max_income = df['monthly_income'].max()
            min_income = df['monthly_income'].min()
            
            projected_yearly = avg_monthly * 12
            
            pred_query = '''
                SELECT * FROM predictions 
                WHERE source = ? 
                ORDER BY prediction_date DESC LIMIT 1
            '''
            latest_pred = pd.read_sql_query(pred_query, conn, params=(source,))
            
            report_data = {
                'source': source,
                'period': f"{start_date} to {end_date}",
                'months': months_count,
                'total_income': float(total_income),
                'avg_monthly': float(avg_monthly),
                'max_income': float(max_income),
                'min_income': float(min_income),
                'projected_yearly': float(projected_yearly),
                'monthly_breakdown': df.to_dict('records'),
            }
            
            if not latest_pred.empty:
                report_data['latest_prediction'] = latest_pred.iloc[0].to_dict()
            
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO reports 
                (report_type, source, start_date, end_date, total_income, 
                 avg_monthly, projected_yearly, report_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', ('DETAILED', source, start_date, end_date, total_income,
                  avg_monthly, projected_yearly, json.dumps(report_data)))
            conn.commit()
            
            conn.close()
            return report_data
        else:
            conn.close()
            return None
    
    def get_source_statistics(self, source):
        """Get comprehensive statistics for specific income source"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                COALESCE(AVG(amount_remitted), 0) as avg_amount,
                COALESCE(MAX(amount_remitted), 0) as max_amount,
                COALESCE(MIN(amount_remitted), 0) as min_amount,
                COUNT(*) as total_records,
                COALESCE(SUM(amount_remitted), 0) as total_revenue
            FROM historical_data 
            WHERE source = ?
        ''', (source,))
        
        hist_stats = cursor.fetchone()
        
        cursor.execute('''
            SELECT 
                COALESCE(AVG(monthly_income), 0) as avg_user_input,
                COALESCE(MAX(monthly_income), 0) as max_user_input,
                COUNT(*) as total_user_inputs,
                COALESCE(SUM(monthly_income), 0) as total_user_income
            FROM user_income_inputs 
            WHERE source = ?
        ''', (source,))
        
        user_stats = cursor.fetchone()
        
        conn.close()
        
        return {
            'historical': {
                'avg_amount': hist_stats[0] or 0,
                'max_amount': hist_stats[1] or 0,
                'min_amount': hist_stats[2] or 0,
                'total_records': hist_stats[3] or 0,
                'total_revenue': hist_stats[4] or 0
            },
            'user_inputs': {
                'avg_input': user_stats[0] or 0,
                'max_input': user_stats[1] or 0,
                'total_inputs': user_stats[2] or 0,
                'total_income': user_stats[3] or 0
            }
        }
    
    def get_all_sources_stats(self):
        """Get statistics for all sources"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                source,
                COUNT(*) as record_count,
                AVG(amount_remitted) as avg_amount,
                SUM(amount_remitted) as total_amount
            FROM historical_data
            GROUP BY source
            ORDER BY total_amount DESC
        ''')
        
        results = cursor.fetchall()
        conn.close()
        
        return [
            {
                'source': r[0],
                'record_count': r[1],
                'avg_amount': r[2],
                'total_amount': r[3]
            }
            for r in results
        ]
    
    def get_historical_data(self, source):
        """Get historical data for a source"""
        conn = sqlite3.connect(self.db_name)
        query = '''
            SELECT date, amount_remitted, year, month
            FROM historical_data
            WHERE source = ?
            ORDER BY date
        '''
        df = pd.read_sql_query(query, conn, params=(source,))
        conn.close()
        return df

    def get_yearly_totals(self, source):
        """
        Return yearly totals for a given source combining:
        - historical_data (Excel-loaded)
        - user_income_inputs (user-entered updates)
        Output: {year_int: total_float}
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()

        totals = {}
        try:
            cursor.execute('''
                SELECT year, SUM(amount_remitted) as total
                FROM historical_data
                WHERE source = ?
                GROUP BY year
                ORDER BY year
            ''', (source,))
            for y, total in cursor.fetchall():
                try:
                    if y is None:
                        continue
                    totals[int(y)] = float(total or 0.0)
                except Exception:
                    continue

            # Add user inputs by calendar year (income_date)
            cursor.execute('''
                SELECT strftime('%Y', income_date) as year, SUM(monthly_income) as total
                FROM user_income_inputs
                WHERE source = ?
                GROUP BY strftime('%Y', income_date)
                ORDER BY year
            ''', (source,))
            for y, total in cursor.fetchall():
                try:
                    if y is None:
                        continue
                    yi = int(y)
                    totals[yi] = float(totals.get(yi, 0.0) + float(total or 0.0))
                except Exception:
                    continue
        finally:
            conn.close()

        return totals

    def get_total_monthly_revenue(self):
        """
        Total monthly revenue across ALL sources, combining:
        - historical_data (Excel-loaded)
        - user_income_inputs (user-entered)

        Returns: { year_int: [12 floats Jan..Dec] }
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        out = {}
        try:
            # Historical totals across all sources
            cursor.execute('''
                SELECT year, month, SUM(amount_remitted) as total
                FROM historical_data
                GROUP BY year, month
                ORDER BY year, month
            ''')
            for y, m, total in cursor.fetchall():
                try:
                    if y is None or m is None:
                        continue
                    yi = int(y)
                    mi = int(m)
                    if not (1 <= mi <= 12):
                        continue
                    if yi not in out:
                        out[yi] = [0.0] * 12
                    out[yi][mi - 1] += float(total or 0.0)
                except Exception:
                    continue

            # Add user inputs grouped by calendar year/month
            cursor.execute('''
                SELECT strftime('%Y', income_date) as year,
                       CAST(strftime('%m', income_date) AS INTEGER) as month,
                       SUM(monthly_income) as total
                FROM user_income_inputs
                GROUP BY strftime('%Y', income_date), strftime('%m', income_date)
                ORDER BY year, month
            ''')
            for y, m, total in cursor.fetchall():
                try:
                    if y is None or m is None:
                        continue
                    yi = int(y)
                    mi = int(m)
                    if not (1 <= mi <= 12):
                        continue
                    if yi not in out:
                        out[yi] = [0.0] * 12
                    out[yi][mi - 1] += float(total or 0.0)
                except Exception:
                    continue
        finally:
            conn.close()

        return out

    def get_all_historical_monthly_totals(self):
        """
        Aggregate Excel-loaded historical_data into monthly totals per source per calendar year.
        Returns: { source: { year_int: [12 floats Jan..Dec] } }
        """
        # Normalize source names from Excel/DB to the same IDs used by the UI.
        # This prevents mismatches like "Bus Ticket 1" vs "BUS-1" causing empty charts/tables.
        source_map = {
            'BUS TICKET 1': 'BUS-1',
            'BUS TICKET1': 'BUS-1',
            'BUS-1': 'BUS-1',
            'BUS TICKET 2': 'BUS-2',
            'BUS TICKET2': 'BUS-2',
            'BUS-2': 'BUS-2',
            'DELIVERY TRUCK': 'DELIVERY TRUCK',
            'MOTORIZED TRICYCLE': 'MOTORIZED VEHICLE',
            'MOTORIZED VEHICLE': 'MOTORIZED VEHICLE',
            'TOILET/LAVATORY': 'TOILET-LAVATORY',
            'TOILET - LAVATORY': 'TOILET-LAVATORY',
            'TOILET LAVATORY': 'TOILET-LAVATORY',
            'TOILET-LAVATORY': 'TOILET-LAVATORY',
            'STREET FOODS': 'STREET FOODS',
            'MARKET LINER': 'LINER-MARKET',
            'LINER-MARKET': 'LINER-MARKET',
            'TABO': 'TABO',
            'STALL RENTAL': 'MARKET-RENTAL STALL-SPACE',
            'STALL RENTAL STALL SPACE': 'MARKET-RENTAL STALL-SPACE',
            'MARKET-RENTAL STALL-SPACE': 'MARKET-RENTAL STALL-SPACE',
            'MARKET RENTAL STALL SPACE': 'MARKET-RENTAL STALL-SPACE',
            'MARKET ELECTRIC': 'MARKET ELECTRIC',
        }

        def canonical_source(s):
            if s is None:
                return None
            t = str(s).strip()
            if not t:
                return None
            u = ' '.join(t.upper().replace('_', ' ').split())
            return source_map.get(u, t)

        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT source, year, month, amount_remitted FROM historical_data')
        out = {}
        for row in cursor.fetchall():
            source, year, month, amt = row
            source = canonical_source(source)
            if not source:
                continue
            try:
                y = int(year) if year is not None else None
                m = int(month) if month is not None else None
            except (TypeError, ValueError):
                continue
            if y is None or m is None or not (1 <= m <= 12):
                continue
            if source not in out:
                out[source] = {}
            if y not in out[source]:
                out[source][y] = [0.0] * 12
            out[source][y][m - 1] += float(amt or 0)
        conn.close()
        return out
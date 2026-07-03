import os
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, Response
from database import Database
from arima_predictor import RevenueARIMAModel as RevenuePredictor
from data_loader import DataLoader
import pandas as pd
from datetime import datetime, timedelta
import json
import io
import csv
import traceback
import threading
import sqlite3
from functools import wraps
import time
import secrets
import smtplib
from email.message import EmailMessage
import hashlib
import math

app = Flask(__name__)
app.secret_key = 'meedo-revenue-system-secret'

# ---------------------------
# Local .env loader (no dependency)
# ---------------------------
def _load_local_dotenv(path: str = '.env'):
    """
    Load KEY=VALUE pairs from a local .env file into os.environ
    (only if the key is not already set).
    This makes SMTP setup easier on Windows without global env vars.
    """
    try:
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            for raw in f.read().splitlines():
                line = str(raw or '').strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if not k:
                    continue
                if os.environ.get(k) is None or os.environ.get(k) == '':
                    os.environ[k] = v
    except Exception:
        # Non-fatal
        return

# Load dotenv early (before reading SMTP env vars)
_load_local_dotenv('.env')

# Dev QoL: auto-reload templates + avoid browser caching during development
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

@app.after_request
def add_no_cache_headers(resp):
    # Prevent stale UI during local development (HTML + static assets)
    p = request.path or ''
    # SSE endpoints must not be forced to "no-store" (can break EventSource in some browsers/proxies)
    if p.startswith('/api/tracker/stream/'):
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp

    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

# Initialize components
db = Database()
predictor = RevenuePredictor()
data_loader = DataLoader()


def _json_sanitize_for_client(obj):
    """
    Recursively replace float nan/inf and unwrap numpy scalars so the payload is
    strict JSON (JavaScript JSON.parse rejects Infinity/NaN). Model CV payloads
    may use inf for failed folds.
    """
    try:
        import numpy as np
    except ImportError:
        np = None

    if np is not None:
        try:
            if isinstance(obj, np.generic):
                obj = obj.item()
        except Exception:
            try:
                obj = float(obj)
            except Exception:
                return None

    if isinstance(obj, dict):
        return {k: _json_sanitize_for_client(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize_for_client(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, datetime):
        try:
            return obj.isoformat(sep=' ', timespec='seconds')
        except Exception:
            return str(obj)
    return obj


def _normalize_email_gmail_only(email_val):
    """Non-empty emails must end with @gmail.com (case-insensitive). Empty clears."""
    if email_val is None:
        return None
    s = str(email_val).strip()
    if not s:
        return ''
    if not s.lower().endswith('@gmail.com'):
        raise ValueError('Enter a Gmail address ending in @gmail.com, or leave email blank.')
    return s

# ---------------------------
# Tracker live updates (SSE)
# ---------------------------
_tracker_event_cond = threading.Condition()
_tracker_event_seq = {}  # key -> int (monotonic)

def _tracker_key(source: str, year: int) -> str:
    return f"{str(source)}::{int(year)}"

def publish_tracker_event(source: str, year: int):
    """Notify all connected clients that tracker data changed for (source, year)."""
    try:
        k = _tracker_key(source, int(year))
        with _tracker_event_cond:
            _tracker_event_seq[k] = int(_tracker_event_seq.get(k, 0)) + 1
            _tracker_event_cond.notify_all()
    except Exception:
        pass

def _get_tracker_seq(source: str, year: int) -> int:
    try:
        return int(_tracker_event_seq.get(_tracker_key(source, year), 0))
    except Exception:
        return 0

# Ticket pads / waiting-list live updates (same condition variable as tracker)
TICKET_PADS_EVENT_KEY = '__TICKET_PADS__'


def publish_ticket_pads_event():
    """Notify clients that ticket stub / waiting-list data changed (shared across roles)."""
    try:
        with _tracker_event_cond:
            _tracker_event_seq[TICKET_PADS_EVENT_KEY] = int(_tracker_event_seq.get(TICKET_PADS_EVENT_KEY, 0)) + 1
            _tracker_event_cond.notify_all()
    except Exception:
        pass


# ---------------------------
# Auth helpers (session-based)
# ---------------------------
def current_user():
    u = session.get('user')
    if not isinstance(u, dict):
        return None
    if not u.get('username'):
        return None
    # Session invalidation: force logout if admin changed role/status
    try:
        uid = int(u.get('id') or 0)
        sv = int(u.get('sv') or 0)
        cur = int(db.get_user_session_version(uid))
        if cur != sv:
            session.pop('user', None)
            return None
    except Exception:
        pass
    return u


def _safe_next_url(raw, default='/'):
    """Reject open redirects and never send logged-in users back to /login."""
    s = (str(raw or '') or default).strip() or default
    if not s.startswith('/') or s.startswith('//'):
        return default
    low = s.lower()
    if low == '/login' or low.startswith('/login?'):
        return default
    return s


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            # For JSON/XHR-style endpoints return 401 (never HTML redirect) so fetch().json()
            # does not follow to /login and try to parse HTML as JSON.
            p = request.path or ''
            xhr_json = (
                p.startswith('/api/')
                or p.startswith('/source-stats')
                or p.startswith('/auto-predict')
                or p.startswith('/batch-predict')
                or p.startswith('/predict')
                or p.startswith('/history/')
                or p.startswith('/yoy-comparison')
                or p.startswith('/all-stats')
                or p.startswith('/generate-report')
                or request.is_json
            )
            if xhr_json:
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if u is None:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        try:
            if not db.is_role_admin(u.get('role')):
                return jsonify({'success': False, 'error': 'Forbidden (admin only)'}), 403
        except Exception:
            if (u.get('role') or '').lower() != 'admin':
                return jsonify({'success': False, 'error': 'Forbidden (admin only)'}), 403
        return fn(*args, **kwargs)
    return wrapper

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Already signed in: do not show login again until explicit logout.
    if current_user() is not None:
        if request.method == 'GET':
            next_url = _safe_next_url(request.args.get('next'), '/')
        else:
            next_url = _safe_next_url((request.form or {}).get('next'), '/')
        return redirect(next_url)

    if request.method == 'GET':
        return render_template('login.html', next=request.args.get('next', '/'))
    data = request.form or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '')
    next_url = _safe_next_url(data.get('next'), '/')
    if '@' in username:
        try:
            _normalize_email_gmail_only(username)
        except ValueError as ve:
            return render_template('login.html', next=next_url, error=str(ve)), 200
    user = db.authenticate_user(username, password)
    if not user:
        try:
            is_active = db.get_user_active_flag(username)
        except Exception:
            is_active = None
        if is_active is False:
            # Return 200 so browsers don't show a red 401 in console for form posts
            return render_template('login.html', next=next_url, error='Account is disabled. Please contact your administrator.'), 200
        # Return 200 so browsers don't show a red 401 in console for form posts
        return render_template('login.html', next=next_url, error='Invalid username, Gmail, or password.'), 200
    session['user'] = {
        'id': user['id'],
        'username': user['username'],
        'role': user.get('role', 'staff'),
        'sv': int(user.get('session_version') or 0),
    }
    try:
        db.touch_last_login(user['id'])
    except Exception:
        pass
    return redirect(next_url)

@app.route('/logout', methods=['POST', 'GET'])
def logout():
    session.pop('user', None)
    if request.method == 'POST' or (request.args.get('next') is None):
        return redirect(url_for('login'))
    return redirect(request.args.get('next'))

@app.route('/api/me', methods=['GET'])
def api_me():
    u = current_user()
    if not u:
        return jsonify({'authenticated': False}), 200
    # Do not expose session-version field to client
    role = u.get('role', 'staff')
    try:
        is_admin = bool(db.is_role_admin(role))
    except Exception:
        is_admin = str(role or '').lower() == 'admin'
    # Include email for account settings UI
    try:
        email = db.get_user_email(int(u.get('id') or 0))
    except Exception:
        email = None
    safe = {'id': u.get('id'), 'username': u.get('username'), 'role': role, 'is_admin': is_admin, 'email': email}
    return jsonify({'authenticated': True, 'user': safe}), 200

# ---------------------------
# Forgot password (security questions)
# ---------------------------
SECURITY_QUESTION_BANK = {
    "hero": "What is your favorite hero?",
    "crush": "What is the name of your first crush?",
    "pet": "What is the name of your first pet?",
    "school": "What is the name of your elementary school?",
    "teacher": "What is the last name of your favorite teacher?",
}

_reset_attempts = {}  # key -> {"n": int, "t": float}

def _attempt_key() -> str:
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    return f"{ip}::{(request.form.get('username') if request.form else '') or (request.args.get('username') if request.args else '')}"

def _rate_limit(max_attempts: int = 8, window_sec: int = 600):
    k = _attempt_key()
    now = time.time()
    cur = _reset_attempts.get(k) or {"n": 0, "t": now}
    # reset window
    if (now - float(cur.get("t") or now)) > window_sec:
        cur = {"n": 0, "t": now}
    cur["n"] = int(cur.get("n") or 0) + 1
    _reset_attempts[k] = cur
    if cur["n"] > max_attempts:
        return False
    return True

@app.route('/api/auth/security-questions', methods=['GET'])
def api_security_questions():
    ident = (request.args.get('identifier') or request.args.get('username') or '').strip()
    if not ident:
        return jsonify({'success': False, 'error': 'Username or email is required'}), 400
    u = db.get_user_by_username_or_email(ident)
    if not u:
        return jsonify({'success': True, 'configured': False, 'exists': False}), 200
    username = str(u.get('username') or '').strip()
    info = db.get_security_questions_by_username(username)
    if info is None:
        return jsonify({'success': True, 'configured': False, 'exists': False}), 200
    if not info.get('is_configured'):
        return jsonify({'success': True, 'configured': False, 'exists': True}), 200
    q1 = str(info.get('q1') or '')
    q2 = str(info.get('q2') or '')
    return jsonify({
        'success': True,
        'configured': True,
        'exists': True,
        'q1': {'id': q1, 'text': SECURITY_QUESTION_BANK.get(q1, q1)},
        'q2': {'id': q2, 'text': SECURITY_QUESTION_BANK.get(q2, q2)},
    }), 200

@app.route('/api/auth/reset-password', methods=['POST'])
def api_reset_password():
    if not _rate_limit():
        # Return 200 so the browser console doesn't show a red 4xx for expected UX errors
        return jsonify({'success': False, 'error': 'Too many attempts. Please wait and try again.'}), 200
    data = request.get_json(silent=True) or {}
    ident = (data.get('identifier') or data.get('username') or '').strip()
    a1 = data.get('answer1') or ''
    a2 = data.get('answer2') or ''
    new_password = data.get('new_password') or ''
    if not ident:
        # Return 200 so the browser console doesn't show a red 4xx for expected UX errors
        return jsonify({'success': False, 'error': 'Username or email is required'}), 200
    try:
        u = db.get_user_by_username_or_email(ident)
        if not u:
            raise ValueError("User not found")
        db.reset_password_by_security(str(u.get('username') or ''), a1, a2, new_password)
        # Notify user if email exists (best-effort, but report status)
        email_notified = False
        email_notify_error = None
        try:
            email = (u.get('email') or '').strip()
            if email:
                _send_password_changed_email(email, username=u.get('username'), reason='Password reset (Security questions)')
                email_notified = True
        except Exception as e:
            email_notify_error = str(e)
            traceback.print_exc()
        resp = {'success': True, 'email_notified': bool(email_notified)}
        try:
            dev_mode = os.environ.get('MEEDO_DEV', '').strip() in ('1', 'true', 'True', 'yes', 'YES')
            if dev_mode and email_notify_error:
                resp['email_notify_error'] = email_notify_error
        except Exception:
            pass
        return jsonify(resp)
    except ValueError as ve:
        # Return 200 so the browser console doesn't show a red 4xx for expected UX errors
        return jsonify({'success': False, 'error': str(ve)}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/verify-security', methods=['POST'])
def api_verify_security():
    """
    Verify security answers (without changing password).
    Used by the login-page forgot-password flow to avoid showing the new password
    field until answers are correct.
    """
    if not _rate_limit():
        return jsonify({'success': False, 'error': 'Too many attempts. Please wait and try again.'}), 200
    data = request.get_json(silent=True) or {}
    ident = (data.get('identifier') or data.get('username') or '').strip()
    a1 = data.get('answer1') or ''
    a2 = data.get('answer2') or ''
    if not ident:
        return jsonify({'success': False, 'error': 'Username or email is required'}), 200
    if not a1 or not a2:
        return jsonify({'success': False, 'error': 'Please answer both security questions.'}), 200
    try:
        u = db.get_user_by_username_or_email(ident)
        if not u:
            return jsonify({'success': False, 'error': 'User not found'}), 200
        username = str(u.get('username') or '')
        ok = bool(db.verify_security_answers(username, a1, a2))
        if not ok:
            return jsonify({'success': False, 'error': 'Security answers are incorrect (or account is disabled).'}), 200
        return jsonify({'success': True}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------
# Forgot password (email reset code)
# ---------------------------
def _smtp_config():
    host = str(os.environ.get('MEEDO_SMTP_HOST', '') or '').strip()
    port = int(os.environ.get('MEEDO_SMTP_PORT', '587') or 587)
    user = str(os.environ.get('MEEDO_SMTP_USER', '') or '').strip()
    pwd = str(os.environ.get('MEEDO_SMTP_PASS', '') or '').strip()
    sender = str(os.environ.get('MEEDO_SMTP_FROM', '') or '').strip() or user
    tls = str(os.environ.get('MEEDO_SMTP_TLS', '1') or '1').strip().lower() not in ('0', 'false', 'no')
    return {"host": host, "port": port, "user": user, "pass": pwd, "from": sender, "tls": tls}

def _smtp_is_configured(cfg: dict) -> bool:
    try:
        return bool(cfg.get('host') and cfg.get('from'))
    except Exception:
        return False

def _send_reset_email(to_email: str, otp: str, username: str | None = None):
    """
    Send a reset code via SMTP.
    Uses env vars:
      - MEEDO_SMTP_HOST, MEEDO_SMTP_PORT, MEEDO_SMTP_USER, MEEDO_SMTP_PASS, MEEDO_SMTP_FROM, MEEDO_SMTP_TLS
    """
    cfg = _smtp_config()
    if not _smtp_is_configured(cfg):
        raise ValueError("Email service is not configured on this server.")
    if not to_email:
        raise ValueError("Email is missing for this account.")

    msg = EmailMessage()
    msg['Subject'] = 'MEEDO Password Reset Code'
    msg['From'] = cfg['from']
    msg['To'] = to_email
    uline = f"\nUsername: {username}\n" if username else "\n"
    msg.set_content(
        "You requested a password reset for your MEEDO account.\n"
        f"{uline}"
        "Use this One-Time Password (OTP) to set a new password:\n\n"
        f"{otp}\n\n"
        "This OTP will expire in 15 minutes.\n"
        "If you did not request this, you can ignore this email.\n"
    )

    server = None
    try:
        server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=12)
        try:
            server.ehlo()
        except Exception:
            pass
        if cfg.get('tls', True):
            server.starttls()
            try:
                server.ehlo()
            except Exception:
                pass
        if cfg.get('user') and cfg.get('pass'):
            server.login(cfg['user'], cfg['pass'])
        server.send_message(msg)
    finally:
        try:
            if server:
                server.quit()
        except Exception:
            pass

def _send_password_changed_email(to_email: str, username: str | None = None, reason: str | None = None):
    """
    Send a security notification when an account password is changed.
    Best-effort: callers should catch SMTP errors and still return success.
    """
    cfg = _smtp_config()
    if not _smtp_is_configured(cfg):
        raise ValueError("Email service is not configured on this server.")
    if not to_email:
        raise ValueError("Email is missing for this account.")

    msg = EmailMessage()
    msg['Subject'] = 'MEEDO Password Changed'
    msg['From'] = cfg['from']
    msg['To'] = to_email
    uline = f"\nUsername: {username}\n" if username else "\n"
    rline = f"Reason: {reason}\n" if reason else ""
    msg.set_content(
        "This is a security notification.\n"
        "Your MEEDO account password was just changed.\n"
        f"{uline}"
        f"{rline}\n"
        "If you did not do this, please contact your administrator immediately.\n"
    )

    server = None
    try:
        server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=12)
        try:
            server.ehlo()
        except Exception:
            pass
        if cfg.get('tls', True):
            server.starttls()
            try:
                server.ehlo()
            except Exception:
                pass
        if cfg.get('user') and cfg.get('pass'):
            server.login(cfg['user'], cfg['pass'])
        server.send_message(msg)
    finally:
        try:
            if server:
                server.quit()
        except Exception:
            pass

@app.route('/api/auth/request-email-reset', methods=['POST'])
def api_request_email_reset():
    if not _rate_limit():
        return jsonify({'success': False, 'error': 'Too many attempts. Please wait and try again.'}), 200
    data = request.get_json(silent=True) or {}
    ident = (data.get('identifier') or '').strip()
    if not ident:
        return jsonify({'success': False, 'error': 'Username or email is required'}), 200

    try:
        user = db.get_user_by_username_or_email(ident)
        if not user:
            return jsonify({'success': False, 'error': 'Email/username not found or not registered.'}), 200
        if int(user.get('is_active') or 0) != 1:
            return jsonify({'success': False, 'error': 'Account is disabled. Please contact your administrator.'}), 200
        email = (user.get('email') or '').strip()
        if not email:
            return jsonify({'success': False, 'error': 'Email not found or not registered for this account. Please ask your admin to add one in Users.'}), 200
        otp = db.create_password_reset_otp(int(user.get('id')), sent_to=email, ttl_minutes=15, digits=6)

        cfg = _smtp_config()
        dev_mode = os.environ.get('MEEDO_DEV', '').strip() in ('1', 'true', 'True', 'yes', 'YES')
        if not _smtp_is_configured(cfg):
            # Dev fallback: return the token so local setups can still test without SMTP.
            if dev_mode:
                return jsonify({'success': True, 'message': 'DEV mode: OTP generated.', 'dev_code': otp}), 200
            return jsonify({'success': False, 'error': 'Email service is not configured. Please contact your administrator.'}), 200

        try:
            _send_reset_email(email, otp, username=user.get('username'))
        except smtplib.SMTPAuthenticationError:
            # Gmail/SMTP: wrong password or missing App Password
            return jsonify({
                'success': False,
                'error': 'Email login failed (SMTP 535). If you are using Gmail, create an App Password and put it in MEEDO_SMTP_PASS, then restart the server.'
            }), 200
        except smtplib.SMTPException:
            return jsonify({
                'success': False,
                'error': 'Email service error. Please verify SMTP settings in .env and restart the server.'
            }), 200
        return jsonify({'success': True, 'message': 'OTP sent to your email.'}), 200
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 200
    except Exception as e:
        traceback.print_exc()
        # Avoid leaking internal errors to the UI
        return jsonify({'success': False, 'error': 'Internal error while sending email. Check server logs.'}), 500

@app.route('/api/auth/verify-email-reset', methods=['POST'])
def api_verify_email_reset():
    """
    Verify email OTP without changing password.
    Used by the login-page forgot-password flow before showing the new password field.
    """
    if not _rate_limit():
        return jsonify({'success': False, 'error': 'Too many attempts. Please wait and try again.'}), 200
    data = request.get_json(silent=True) or {}
    token = (data.get('code') or data.get('token') or '').strip()
    if not token:
        return jsonify({'success': False, 'error': 'Reset code is required'}), 200
    try:
        db.verify_password_reset_token(token)
        return jsonify({'success': True}), 200
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/confirm-email-reset', methods=['POST'])
def api_confirm_email_reset():
    if not _rate_limit():
        return jsonify({'success': False, 'error': 'Too many attempts. Please wait and try again.'}), 200
    data = request.get_json(silent=True) or {}
    token = (data.get('code') or data.get('token') or '').strip()
    new_password = (data.get('new_password') or '').strip()
    try:
        uid = db.consume_password_reset_token(token, new_password)
        # Notify user if email exists (best-effort, but report status)
        email_notified = False
        email_notify_error = None
        try:
            email = db.get_user_email(int(uid)) or ''
            if email:
                _send_password_changed_email(email, reason='Password reset (Email OTP)')
                email_notified = True
        except Exception as e:
            email_notify_error = str(e)
            traceback.print_exc()
        resp = {'success': True, 'email_notified': bool(email_notified)}
        try:
            dev_mode = os.environ.get('MEEDO_DEV', '').strip() in ('1', 'true', 'True', 'yes', 'YES')
            if dev_mode and email_notify_error:
                resp['email_notify_error'] = email_notify_error
        except Exception:
            pass
        return jsonify(resp)
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/my/security-questions', methods=['GET', 'POST'])
@login_required
def api_set_my_security_questions():
    """
    Logged-in users (any role) can set/update their own security questions.
    """
    if request.method == 'GET':
        u = current_user() or {}
        try:
            info = db.get_security_questions_by_user_id(int(u.get('id') or 0))
            if info is None:
                return jsonify({'success': False, 'error': 'User not found'}), 404
            return jsonify({'success': True, 'configured': bool(info.get('is_configured')), 'q1': info.get('q1'), 'q2': info.get('q2')}), 200
        except Exception as e:
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    data = request.get_json(silent=True) or {}
    q1 = data.get('q1')
    q2 = data.get('q2')
    a1 = data.get('a1')
    a2 = data.get('a2')
    u = current_user() or {}
    try:
        db.set_security_questions(int(u.get('id')), q1, a1, q2, a2)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/my/email', methods=['GET', 'POST'])
@login_required
def api_my_email():
    u = current_user() or {}
    uid = int(u.get('id') or 0)
    if request.method == 'GET':
        try:
            return jsonify({'success': True, 'email': db.get_user_email(uid)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    # POST
    data = request.get_json(silent=True) or {}
    email = data.get('email', None)
    try:
        email = _normalize_email_gmail_only(email)
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    try:
        db.set_user_email(uid, email)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email already exists.'}), 400
    except sqlite3.OperationalError as oe:
        if 'locked' in str(oe).lower():
            return jsonify({'success': False, 'error': 'Database is busy. Please try again.'}), 200
        return jsonify({'success': False, 'error': str(oe)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------
# Roles management (admin-only)
# ---------------------------
@app.route('/api/roles', methods=['GET'])
@admin_required
def api_list_roles():
    try:
        include_inactive = str(request.args.get('include_inactive', '') or '').strip().lower() in ('1', 'true', 'yes')
        return jsonify({'success': True, 'roles': db.list_roles(include_inactive=include_inactive)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/roles', methods=['POST'])
@admin_required
def api_create_role():
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name')
        is_admin = bool(data.get('is_admin', False))
        is_active = bool(data.get('is_active', True))
        db.create_role(name=name, is_admin=is_admin, is_active=is_active)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Role name already exists.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/roles/<int:role_id>', methods=['PATCH'])
@admin_required
def api_update_role(role_id):
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', None)
        is_admin = data.get('is_admin', None)
        if is_admin is not None:
            is_admin = bool(is_admin)
        db.update_role(role_id, name=name, is_admin=is_admin)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Role name already exists.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/roles/<int:role_id>/active', methods=['POST'])
@admin_required
def api_set_role_active(role_id):
    try:
        data = request.get_json(silent=True) or {}
        is_active = data.get('is_active', None)
        if is_active is None:
            raise ValueError("is_active is required")
        db.set_role_active(role_id, bool(is_active))
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/roles/<int:role_id>', methods=['DELETE'])
@admin_required
def api_delete_role(role_id):
    try:
        db.delete_role(role_id)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/my/password', methods=['POST'])
@login_required
def api_change_my_password():
    try:
        data = request.get_json(silent=True) or {}
        old_password = data.get('old_password') or ''
        new_password = data.get('new_password') or ''
        u = current_user() or {}
        db.change_my_password(int(u.get('id')), old_password, new_password)
        # Notify user if email exists (best-effort, but report status)
        email_notified = False
        email_notify_error = None
        try:
            uid = int(u.get('id') or 0)
            email = db.get_user_email(uid) or ''
            if email:
                _send_password_changed_email(email, username=u.get('username'), reason='Password change (Account)')
                email_notified = True
        except Exception as e:
            email_notify_error = str(e)
            traceback.print_exc()
        resp = {'success': True, 'email_notified': bool(email_notified)}
        try:
            dev_mode = os.environ.get('MEEDO_DEV', '').strip() in ('1', 'true', 'True', 'yes', 'YES')
            if dev_mode and email_notify_error:
                resp['email_notify_error'] = email_notify_error
        except Exception:
            pass
        return jsonify(resp)
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# Protect pages by default
@app.before_request
def _auth_gate():
    # Allow static, login, favicon, and health-like API
    p = request.path or ''
    if p.startswith('/static/') or p in ('/login', '/favicon.ico', '/api/me'):
        return None
    # Allow app startup endpoints only after login
    if p == '/':
        if current_user() is None:
            return redirect(url_for('login', next='/'))
    return None

# Initialize on startup
training_results = None
init_in_progress = False
init_last_error = None

def initialize_system():
    """Load data and train models on startup"""
    print("Loading historical data...")
    try:
        historical_data = data_loader.load_all_data()
        
        if historical_data is not None and not historical_data.empty:
            # Save to database
            db.save_historical_data(historical_data)
            
            # Train models
            print("Training models for each income source...")
            results = predictor.train_models(historical_data)
            
            # Print results
            for source, result in results.items():
                if result['success']:
                    print(f"[OK] {source}: Trained with {result['data_points']} data points")
                else:
                    print(f"[ERROR] {source}: {result.get('message', 'Training failed')}")
            
            return results
        else:
            print("No historical data loaded. Using sample data...")
            sample_data = data_loader.create_sample_data()
            results = predictor.train_models(sample_data)
            return results
    except Exception as e:
        print(f"Error initializing system: {e}")
        traceback.print_exc()
        return {}

def _init_system_background():
    """Run initialize_system without blocking server startup."""
    global training_results, init_in_progress, init_last_error
    init_in_progress = True
    init_last_error = None
    try:
        print("="*60)
        print("INITIALIZING REVENUE PREDICTION SYSTEM (background)")
        print("="*60)
        training_results = initialize_system()
        print("[OK] Background initialization finished.")
    except Exception as e:
        init_last_error = str(e)
        traceback.print_exc()
        print(f"[ERROR] Background initialization failed: {e}")
        training_results = {}
    finally:
        init_in_progress = False

@app.route('/api/historical-monthly')
@login_required
def api_historical_monthly():
    """Monthly totals per source per year from SQLite (Excel / historical load)."""
    try:
        raw = db.get_all_historical_monthly_totals()
        # JSON-friendly: year keys as strings, months as lists
        payload = {}
        for src, years in raw.items():
            payload[src] = {str(y): months for y, months in years.items()}
        return jsonify(payload)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/total-monthly')
@login_required
def api_total_monthly():
    """Total monthly revenue across all sources per year."""
    try:
        raw = db.get_total_monthly_revenue()
        payload = {str(y): months for y, months in raw.items()}
        return jsonify(payload)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/tracker-monthly')
@login_required
def api_tracker_monthly():
    """Monthly totals per source per year from shared tracker_monthly table."""
    try:
        raw = db.get_all_tracker_monthly_totals()
        payload = {}
        for src, years in raw.items():
            payload[src] = {str(y): months for y, months in years.items()}
        return jsonify(payload)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/tracker/reset-year', methods=['POST'])
@admin_required
def api_reset_tracker_year():
    """Admin-only: wipe shared tracker_monthly entries for a year (e.g., 2026 reset)."""
    try:
        data = request.get_json(silent=True) or {}
        year = data.get('year', None)
        source = data.get('source', None)
        if year is None:
            raise ValueError("year is required")
        db.reset_tracker_year(int(year), source=source)
        # Publish events so open pages update immediately
        if source:
            publish_tracker_event(str(source), int(year))
        else:
            try:
                # Broadcast to any known sources for that year
                raw = db.get_all_tracker_monthly_totals()
                for src in (raw.keys() if isinstance(raw, dict) else []):
                    publish_tracker_event(str(src), int(year))
            except Exception:
                pass
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------
# Shared tracker entries (all logged-in users)
# ---------------------------
@app.route('/api/tracker/<path:source>/<int:year>', methods=['GET'])
@login_required
def api_get_tracker_months(source, year):
    try:
        data = db.get_tracker_months(source, int(year))
        return jsonify({'success': True, 'source': source, 'year': int(year), 'months': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tracker/<path:source>/<int:year>/daily', methods=['GET'])
@login_required
def api_get_tracker_daily_year(source, year):
    """Shared day-by-day peso/ticket maps for all months in a tracker year (2026+ UI)."""
    try:
        raw = db.get_tracker_daily_for_year(source, int(year))
        # JSON keys must be strings for stable clients; include who last saved daily detail
        daily = {}
        for k, v in raw.items():
            cell = {'peso': v['peso'], 'ticket': v['ticket']}
            if v.get('updated_by') is not None:
                cell['updated_by'] = v['updated_by']
            if v.get('updated_role') is not None:
                cell['updated_role'] = v['updated_role']
            if v.get('updated_at') is not None:
                cell['updated_at'] = v['updated_at']
            if v.get('peso_audit'):
                cell['peso_audit'] = v['peso_audit']
            if v.get('ticket_audit'):
                cell['ticket_audit'] = v['ticket_audit']
            daily[str(k)] = cell
        return jsonify({'success': True, 'source': source, 'year': int(year), 'daily': daily})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tracker/<path:source>/<int:year>/<int:month>/daily', methods=['POST'])
@login_required
def api_upsert_tracker_daily_month(source, year, month):
    """Persist daily breakdown so all roles see the same details (not browser-local only)."""
    try:
        data = request.get_json(silent=True) or {}
        peso = data.get('peso', None)
        ticket = data.get('ticket', None)
        if peso is None:
            peso = {}
        if ticket is None:
            ticket = {}
        peso_audit = data.get('peso_audit', None)
        ticket_audit = data.get('ticket_audit', None)
        if peso_audit is None:
            peso_audit = {}
        if ticket_audit is None:
            ticket_audit = {}
        u = current_user() or {}
        acting_username = u.get('username', 'user')
        acting_role = u.get('role', 'staff')
        entry = db.upsert_tracker_daily_month(
            source,
            int(year),
            int(month),
            peso,
            ticket,
            acting_username,
            acting_role,
            peso_audit_map=peso_audit,
            ticket_audit_map=ticket_audit,
        )
        publish_tracker_event(source, int(year))
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'entry': entry})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tracker/<path:source>/<int:year>/<int:month>', methods=['POST'])
@login_required
def api_upsert_tracker_month(source, year, month):
    try:
        data = request.get_json(silent=True) or {}
        amount = data.get('amount', None)
        if amount is None:
            raise ValueError("amount is required")
        ts = data.get('tickets_sold', None)
        pid = data.get('ticket_pad_id', None)
        if ts is not None:
            ts = int(ts)
        if pid is not None:
            pid = int(pid)
        u = current_user() or {}
        acting_username = u.get('username', 'user')
        acting_role = u.get('role', 'staff')
        rem = data.get('revenue_entry_mode', None)
        entry = db.upsert_tracker_month(
            source,
            int(year),
            int(month),
            float(amount),
            acting_username,
            acting_role,
            tickets_sold=ts,
            ticket_pad_id=pid,
            revenue_entry_mode=rem,
        )
        publish_tracker_event(source, int(year))
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'entry': entry})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tracker/<path:source>/<int:year>/<int:month>', methods=['DELETE'])
@login_required
def api_delete_tracker_month(source, year, month):
    """Remove saved revenue for one calendar month (headline + daily breakdown). Month index 0–11."""
    try:
        deleted = db.delete_tracker_month(source, int(year), int(month))
        publish_tracker_event(source, int(year))
        return jsonify({'success': True, 'deleted': bool(deleted)})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# --- Waiting list (pool) API must be registered BEFORE /api/ticket-pads/<path:source>
# or URLs like /api/ticket-pads-pool are captured as source="pool" (wrong handler → 404).
@app.route('/api/ticket-pads/pool/<int:pad_id>/assign', methods=['POST'])
@app.route('/api/ticket-pads-pool/<int:pad_id>/assign', methods=['POST'])
@login_required
def api_ticket_pads_pool_assign(pad_id):
    """Assign an unassigned stub to an income category (recomputes pad value from category ticket price)."""
    try:
        data = request.get_json(silent=True) or {}
        tgt = data.get('target_source') or data.get('source') or ''
        pad = db.assign_ticket_pad_from_pool(int(pad_id), str(tgt))
        try:
            publish_tracker_event(str(tgt), 2026)
        except Exception:
            pass
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'pad': pad})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ticket-pads/pool/<int:pad_id>', methods=['PATCH', 'DELETE'])
@app.route('/api/ticket-pads-pool/<int:pad_id>', methods=['PATCH', 'DELETE'])
@admin_required
def api_ticket_pads_pool_one(pad_id):
    """Update or delete an unassigned stub in the pool (admin only)."""
    pool = db.TICKET_PAD_POOL_SOURCE
    try:
        if request.method == 'DELETE':
            db.delete_ticket_pad(pool, int(pad_id))
            try:
                publish_ticket_pads_event()
            except Exception:
                pass
            return jsonify({'success': True})
        data = request.get_json(silent=True) or {}
        bn = data.get('book_number', None)
        tc = data.get('ticket_count', None)
        book_arg = None if bn is None else str(bn)
        count_arg = None if tc is None else int(tc)
        if book_arg is None and count_arg is None:
            raise ValueError("Provide book_number and/or ticket_count to update.")
        updated = db.update_ticket_pad(pool, int(pad_id), book_number=book_arg, ticket_count=count_arg)
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'pad': updated})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ticket-pads/pool', methods=['GET', 'POST'])
@app.route('/api/ticket-pads-pool', methods=['GET', 'POST'])
@login_required
def api_ticket_pads_pool():
    """List or create ticket stubs not yet assigned to an income category."""
    try:
        if request.method == 'GET':
            pads = db.get_unassigned_ticket_pads()
            resp = jsonify({'success': True, 'pads': pads})
            resp.headers['Cache-Control'] = 'no-store, max-age=0'
            return resp
        data = request.get_json(silent=True) or {}
        book_number = data.get('book_number') or ''
        ticket_count = data.get('ticket_count', None)
        if ticket_count is None:
            raise ValueError("ticket_count is required")
        created = db.add_unassigned_ticket_pad(str(book_number), int(ticket_count))
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'pad': created})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ticket-pads/wait')
@login_required
def api_ticket_pads_wait():
    """Long-poll: returns when ticket pad / waiting-list data changes (for live UI sync)."""
    try:
        since = int(request.args.get('since', '0') or 0)
    except Exception:
        since = 0
    with _tracker_event_cond:
        cur = int(_tracker_event_seq.get(TICKET_PADS_EVENT_KEY, 0))
        if cur != since:
            return jsonify({'success': True, 'seq': cur})
        _tracker_event_cond.wait(timeout=25)
        cur2 = int(_tracker_event_seq.get(TICKET_PADS_EVENT_KEY, 0))
    return jsonify({'success': True, 'seq': cur2})


@app.route('/api/ticket-pads/check-book', methods=['GET'])
@login_required
def api_ticket_pads_check_book():
    """Return whether a stub/book number is already used (waiting list or any income category)."""
    try:
        q = (request.args.get('q') or request.args.get('book') or '').strip()
        if not q:
            return jsonify({'success': True, 'exists': False})
        info = db.get_ticket_pad_stub_global_info(q)
        if not info:
            return jsonify({'success': True, 'exists': False})
        return jsonify(
            {
                'success': True,
                'exists': True,
                'in_pool': bool(info.get('in_pool')),
                'source': info.get('source') or '',
                'pad_id': int(info.get('id') or 0),
            }
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ticket-pads/<path:source>/<int:pad_id>', methods=['PATCH', 'DELETE'])
@admin_required
def api_ticket_pad_one(source, pad_id):
    """Update or delete a ticket pad (admin only). Must be registered before /api/ticket-pads/<path:source>."""
    try:
        if request.method == 'DELETE':
            db.delete_ticket_pad(source, int(pad_id))
            try:
                publish_ticket_pads_event()
            except Exception:
                pass
            return jsonify({'success': True})
        data = request.get_json(silent=True) or {}
        bn = data.get('book_number', None)
        tc = data.get('ticket_count', None)
        book_arg = None if bn is None else str(bn)
        count_arg = None if tc is None else int(tc)
        if book_arg is None and count_arg is None:
            raise ValueError("Provide book_number and/or ticket_count to update.")
        updated = db.update_ticket_pad(source, int(pad_id), book_number=book_arg, ticket_count=count_arg)
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'pad': updated})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ticket-pads/<path:source>', methods=['GET', 'POST'])
@login_required
def api_ticket_pads(source):
    """List or register numbered ticket pads (finite ticket counts per book/stub)."""
    try:
        if request.method == 'GET':
            pads = db.get_ticket_pads_for_source(source)
            return jsonify({'success': True, 'pads': pads})
        data = request.get_json(silent=True) or {}
        book_number = data.get('book_number') or ''
        ticket_count = data.get('ticket_count', None)
        if ticket_count is None:
            raise ValueError("ticket_count is required")
        created = db.add_ticket_pad(source, str(book_number), int(ticket_count))
        try:
            publish_ticket_pads_event()
        except Exception:
            pass
        return jsonify({'success': True, 'pad': created})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tracker/stream/<path:source>/<int:year>')
@login_required
def api_tracker_stream(source, year):
    """
    Server-Sent Events stream.
    Emits an event when tracker data changes for (source, year).
    """
    key = _tracker_key(source, int(year))

    def gen():
        # Start from current seq so new clients don't immediately fire unless something changes
        with _tracker_event_cond:
            last = int(_tracker_event_seq.get(key, 0))
        # Initial handshake event
        yield "event: ready\ndata: ok\n\n"
        while True:
            try:
                with _tracker_event_cond:
                    _tracker_event_cond.wait(timeout=25)
                    cur = int(_tracker_event_seq.get(key, 0))
                if cur != last:
                    last = cur
                    yield f"event: tick\ndata: {last}\n\n"
                else:
                    # Keep-alive to prevent proxies from closing the connection
                    yield "event: keepalive\ndata: 1\n\n"
            except GeneratorExit:
                break
            except Exception:
                break

    resp = Response(gen(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp

@app.route('/api/tracker/<path:source>/<int:year>/rev', methods=['GET'])
@login_required
def api_tracker_rev(source, year):
    """Current live-sync revision for (source, year) — used by long-poll clients."""
    try:
        return jsonify({'success': True, 'seq': _get_tracker_seq(source, int(year))})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tracker/wait/<path:source>/<int:year>')
@login_required
def api_tracker_wait(source, year):
    """
    Long-poll endpoint: waits until tracker changes for (source, year) or timeout.
    Query param:
      - since: last seen seq int
    """
    try:
        since = int(request.args.get('since', '0') or 0)
    except Exception:
        since = 0
    key = _tracker_key(source, int(year))
    with _tracker_event_cond:
        cur = int(_tracker_event_seq.get(key, 0))
        if cur != since:
            return jsonify({'success': True, 'seq': cur})
        _tracker_event_cond.wait(timeout=25)
        cur2 = int(_tracker_event_seq.get(key, 0))
    return jsonify({'success': True, 'seq': cur2})


@app.route('/')
@login_required
def index():
    """Main page"""
    try:
        sources = db.get_categories(active_only=True)
    except Exception:
        sources = []
    return render_template('index.html', sources=sources)

@app.route('/favicon.ico')
def favicon():
    # Avoid noisy 404s in browser console during local dev
    return ('', 204)

@app.route('/api/categories', methods=['GET'])
@login_required
def api_categories():
    """Return income categories."""
    try:
        include_inactive = str(request.args.get('include_inactive', '') or '').strip().lower() in ('1', 'true', 'yes')
        return jsonify({'success': True, 'categories': db.get_categories(active_only=(not include_inactive))})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/categories/<category_id>', methods=['PATCH'])
@admin_required
def api_update_category(category_id):
    """Update a category (name/icon/ticket unit price)."""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', None)
        icon = data.get('icon', None)
        kw = {'name': name, 'icon': icon}
        if 'ticket_unit_php' in data:
            raw_t = data.get('ticket_unit_php')
            if raw_t is None or raw_t == '':
                kw['ticket_unit_php'] = 0.0
            else:
                kw['ticket_unit_php'] = float(raw_t)
        updated = db.update_category(category_id, **kw)
        return jsonify({'success': True, 'category': updated})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Duplicate category name. Please choose a different name.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/categories/<category_id>/active', methods=['POST'])
@admin_required
def api_toggle_category_active(category_id):
    """Enable/disable a category (soft delete)."""
    try:
        data = request.get_json(silent=True) or {}
        is_active = data.get('is_active', None)
        if is_active is None:
            raise ValueError("is_active is required")
        updated = db.set_category_active(category_id, bool(is_active))
        return jsonify({'success': True, 'category': updated})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Duplicate category name. Please choose a different name.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/categories/<category_id>/remove', methods=['POST'])
@admin_required
def api_remove_category(category_id):
    """Remove a category from UI lists (soft-delete)."""
    try:
        removed = db.remove_category(category_id)
        return jsonify({'success': True, 'category': removed})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/categories', methods=['POST'])
@admin_required
def api_add_category():
    """Add a new income category."""
    try:
        # Use silent=True so invalid/missing JSON returns None (not 500)
        data = request.get_json(silent=True) or {}
        name = data.get('name') or ''
        icon = data.get('icon') or ''
        # Auto-generate ID from name (unique)
        created = db.add_category_auto(name, icon)
        return jsonify({'success': True, 'category': created})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        # Fallback if DB unique index fires
        return jsonify({'success': False, 'error': 'Duplicate category name. Please choose a different name.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------
# User management (admin-only)
# ---------------------------
@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    try:
        return jsonify({'success': True, 'users': db.list_users()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    try:
        data = request.get_json(silent=True) or {}
        username = data.get('username')
        email = data.get('email', None)
        password = data.get('password')
        role = data.get('role', 'staff')
        is_active = data.get('is_active', True)
        try:
            email = _normalize_email_gmail_only(email)
        except ValueError as ve:
            return jsonify({'success': False, 'error': str(ve)}), 400
        uid = db.create_user(username=username, password=password, role=role, is_active=bool(is_active), email=email)
        # Optional: initialize security questions on create
        try:
            q1 = (data.get('sec_q1') or '').strip()
            q2 = (data.get('sec_q2') or '').strip()
            a1 = (data.get('sec_a1') or '').strip()
            a2 = (data.get('sec_a2') or '').strip()
            if q1 or q2 or a1 or a2:
                db.set_security_questions(uid, q1, a1, q2, a2)
        except ValueError:
            # If invalid, surface as 400 so admin can correct inputs
            raise
        except Exception:
            pass
        return jsonify({'success': True, 'id': uid})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        # Could be username or email unique index
        return jsonify({'success': False, 'error': 'Username or email already exists.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>', methods=['PATCH'])
@admin_required
def api_update_user(user_id):
    try:
        data = request.get_json(silent=True) or {}
        role = data.get('role', None)
        email = data.get('email', None)
        if 'email' in data:
            try:
                email = _normalize_email_gmail_only(email)
            except ValueError as ve:
                return jsonify({'success': False, 'error': str(ve)}), 400
        db.update_user(user_id, role=role, email=email)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email already exists.'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/active', methods=['POST'])
@admin_required
def api_set_user_active(user_id):
    try:
        data = request.get_json(silent=True) or {}
        is_active = data.get('is_active', None)
        if is_active is None:
            raise ValueError("is_active is required")
        acting = (current_user() or {}).get('id', None)
        db.set_user_active(user_id, bool(is_active), acting_user_id=acting)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/password', methods=['POST'])
@admin_required
def api_reset_user_password(user_id):
    try:
        data = request.get_json(silent=True) or {}
        new_password = data.get('password', None)
        if not new_password:
            raise ValueError("password is required")
        acting = (current_user() or {}).get('id', None)
        db.reset_user_password(user_id, new_password, acting_user_id=acting)
        # Notify user if email exists (best-effort)
        try:
            email = db.get_user_email(int(user_id)) or ''
            if email:
                _send_password_changed_email(email, reason='Password reset (Admin)')
        except Exception:
            traceback.print_exc()
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_delete_user(user_id):
    try:
        acting = (current_user() or {}).get('id', None)
        db.delete_user(user_id, acting_user_id=acting)
        return jsonify({'success': True})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/predict', methods=['POST'])
@login_required
def predict():
    """Handle prediction requests - MONTHLY and YEARLY only"""
    try:
        data = request.get_json()
        source = data.get('source')
        monthly_income = float(data.get('monthly_income', 0))
        notes = data.get('notes', '')
        
        if not source:
            return jsonify({'error': 'Please select an income source'}), 400
        
        if monthly_income <= 0:
            return jsonify({'error': 'Please enter a valid monthly income amount (greater than 0)'}), 400
        
        print(f"Processing prediction for {source} with monthly income: PHP {monthly_income:,.2f}")
        
        # Save user's income input to database
        income_date = datetime.now().date()
        u = current_user() or {}
        created_by = u.get('username', 'user')
        db.save_user_income(source, monthly_income, income_date, notes, created_by=created_by)
        
        # Add to model's memory for learning
        predictor.add_user_input(source, income_date, monthly_income)
        
        # Get prediction (MONTHLY and YEARLY only)
        prediction = predictor.predict_revenue(source, monthly_income)
        
        if not prediction['success']:
            return jsonify({'error': prediction.get('error', 'Prediction failed')}), 500
        
        # Save prediction to database
        db.save_prediction(
            source=source,
            input_income=monthly_income,
            predicted_monthly=prediction['predicted_monthly'],
            predicted_yearly=prediction['predicted_yearly'],
            confidence=prediction['confidence_score'],
        )
        
        # Get source statistics
        stats = db.get_source_statistics(source)
        
        # Get recent user inputs for this source (last 12 months)
        recent_inputs = db.get_user_inputs(source, months=12)
        recent_inputs_list = recent_inputs.to_dict('records') if not recent_inputs.empty else []
        
        # Get model stats
        model_stats = predictor.get_model_stats(source)
        
        result = {
            'success': True,
            'source': source,
            'monthly_income': monthly_income,
            'predicted_monthly': prediction['predicted_monthly'],
            'predicted_yearly': prediction['predicted_yearly'],
            'monthly_predictions': prediction['monthly_predictions'],
            'historical_avg': prediction['historical_avg'],
            'confidence_score': prediction['confidence_score'],
            'ratio': prediction['ratio'],
            'stats': stats,
            'recent_inputs': recent_inputs_list[:6],
            'model_learning': {
                'user_inputs_used': prediction.get('user_inputs_count', 0),
                'model_trained': prediction.get('model_trained', 'N/A')
            },
            'imputation': (model_stats.get('imputation') if isinstance(model_stats, dict) else None),
        }
        
        return jsonify(result)
        
    except ValueError as e:
        print(f"Value error: {e}")
        return jsonify({'error': 'Invalid amount. Please enter a valid number.'}), 400
    except Exception as e:
        print(f"Prediction error: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/auto-predict/<path:source>', methods=['GET'])
@login_required
def auto_predict(source):
    """Auto-predict for a source without requiring user input"""
    try:
        # Get auto-prediction from the model
        prediction = predictor.predict_auto(source)
        
        if not prediction['success']:
            return jsonify({'error': prediction.get('error', 'Prediction failed')}), 500
        
        # Get source statistics
        stats = db.get_source_statistics(source)
        model_stats = predictor.get_model_stats(source)

        # SIMPLIFIED: Don't process historical data for grouping - just return basic stats
        # This avoids the datetime error entirely
        historical_monthly = {}
        yoy_data = {}
        
        result = {
            'success': True,
            'source': source,
            'current_monthly': prediction['current_monthly'],
            'predicted_monthly': prediction['predicted_monthly'],
            'predicted_yearly': prediction['predicted_yearly'],
            'monthly_predictions': prediction['monthly_predictions'],
            'historical_avg': prediction['historical_avg'],
            'confidence_score': prediction['confidence_score'],
            'ratio': prediction['ratio'],
            'stats': stats,
            'historical_monthly': historical_monthly,
            'yoy_data': yoy_data,
            'auto_generated': True,
            'imputation': (model_stats.get('imputation') if isinstance(model_stats, dict) else None),
        }
        
        return jsonify(result)
        
    except Exception as e:
        print(f"Auto-prediction error: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/batch-predict', methods=['POST'])
@login_required
def batch_predict():
    """Predict for multiple sources at once - MONTHLY only"""
    try:
        data = request.get_json()
        sources = data.get('sources', [])
        monthly_income = float(data.get('monthly_income', 0))
        
        if not sources:
            return jsonify({'error': 'No sources selected'}), 400
        
        if monthly_income <= 0:
            return jsonify({'error': 'Please enter a valid monthly income amount'}), 400
        
        results = {}
        for source in sources:
            prediction = predictor.predict_revenue(source, monthly_income)
            if prediction['success']:
                results[source] = {
                    'predicted_yearly': prediction['predicted_yearly'],
                    'confidence': prediction['confidence_score'],
                    'predicted_monthly': prediction['predicted_monthly']
                }
            else:
                results[source] = {
                    'error': prediction.get('error', 'Prediction failed')
                }
        
        return jsonify({
            'success': True,
            'results': results,
            'total_sources': len(results)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/yoy-comparison/<source>')
@login_required
def yoy_comparison(source):
    """Get year-over-year comparison (yearly totals + growth rates)"""
    try:
        yearly_totals = db.get_yearly_totals(source)
        years_sorted = sorted(yearly_totals.keys())

        growth_rates = {}
        growth_values = []
        for i in range(1, len(years_sorted)):
            prev_y = years_sorted[i - 1]
            cur_y = years_sorted[i]
            prev_total = float(yearly_totals.get(prev_y, 0.0))
            cur_total = float(yearly_totals.get(cur_y, 0.0))

            if prev_total <= 0:
                rate = None
            else:
                rate = ((cur_total - prev_total) / prev_total) * 100.0
                growth_values.append(rate)

            growth_rates[str(cur_y)] = rate

        avg_growth = float(sum(growth_values) / len(growth_values)) if growth_values else 0.0

        return jsonify({
            'success': True,
            'source': source,
            'yearly_data': {str(y): float(yearly_totals[y]) for y in years_sorted},
            'growth_rates': growth_rates,
            'avg_growth': avg_growth
        })
    except Exception as e:
        print(f"YoY error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/generate-report', methods=['POST'])
@login_required
def generate_report():
    """Generate comprehensive report for a date range"""
    try:
        data = request.get_json()
        source = data.get('source')
        months = int(data.get('months', 12))
        format_type = data.get('format', 'csv')
        
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=months*30)
        
        report = db.generate_report(source, start_date, end_date)
        
        if not report:
            return jsonify({'success': False, 'error': 'No data found for this period'})
        
        if format_type == 'csv':
            return export_csv(report, source, start_date, end_date)
        elif format_type == 'json':
            return jsonify({'success': True, 'report': report})
        else:
            return jsonify({'success': True, 'report': report})
            
    except Exception as e:
        print(f"Report generation error: {e}")
        return jsonify({'error': str(e)}), 500

def export_csv(report, source, start_date, end_date):
    """Export report as CSV"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['MEEDO Revenue Report'])
    writer.writerow([f'Source: {source}'])
    writer.writerow([f'Period: {report["period"]}'])
    writer.writerow([])
    writer.writerow(['Summary Statistics'])
    writer.writerow(['Total Months', report['months']])
    writer.writerow(['Total Income', f'₱{report["total_income"]:,.2f}'])
    writer.writerow(['Average Monthly', f'₱{report["avg_monthly"]:,.2f}'])
    writer.writerow(['Max Monthly', f'₱{report["max_income"]:,.2f}'])
    writer.writerow(['Min Monthly', f'₱{report["min_income"]:,.2f}'])
    writer.writerow(['Projected Yearly', f'₱{report["projected_yearly"]:,.2f}'])
    
    writer.writerow([])
    writer.writerow(['Monthly Breakdown'])
    writer.writerow(['Date', 'Income', 'Notes'])
    
    for month in report['monthly_breakdown']:
        writer.writerow([
            month['income_date'], 
            f'₱{month["monthly_income"]:,.2f}',
            month.get('notes', '')
        ])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'report_{source}_{start_date}_to_{end_date}.csv'
    )

@app.route('/history/<source>', methods=['GET'])
@login_required
def get_history(source):
    """Get prediction history for a source"""
    try:
        limit = request.args.get('limit', 100, type=int)
        
        predictions = db.get_predictions_history(source, limit=20)
        user_inputs = db.get_user_inputs(source, limit=limit)
        all_inputs = db.get_user_inputs(source, limit=1000)
        
        return jsonify({
            'success': True,
            'predictions': predictions.to_dict('records') if not predictions.empty else [],
            'user_inputs': user_inputs.to_dict('records') if not user_inputs.empty else [],
            'total_inputs': len(all_inputs) if not all_inputs.empty else 0,
            'displayed_inputs': len(user_inputs) if not user_inputs.empty else 0,
            'limit_used': limit
        })
    except Exception as e:
        print(f"History error: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/source-stats/<path:source>')
@login_required
def source_stats(source):
    """Get statistics for specific source"""
    try:
        stats = db.get_source_statistics(source)

        through_year = request.args.get('through_year', type=int)
        if through_year is not None:
            model_stats = predictor.get_model_stats_through_year(source, through_year)
        else:
            model_stats = predictor.get_model_stats(source)
        if model_stats:
            stats['model'] = model_stats

        return jsonify(_json_sanitize_for_client(stats))
    except Exception as e:
        print(f"Stats error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/all-stats')
@login_required
def all_stats():
    """Get statistics for all sources"""
    try:
        stats = db.get_all_sources_stats()
        return jsonify(stats)
    except Exception as e:
        print(f"All stats error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/retrain', methods=['POST'])
@admin_required
def retrain():
    """Retrain models with updated data"""
    global training_results
    try:
        all_sources = ['BUS-1', 'BUS-2', 'DELIVERY TRUCK', 'MOTORIZED VEHICLE', 
                       'TOILET-LAVATORY', 'STREET FOODS', 'LINER-MARKET', 'TABO',
                       'MARKET-RENTAL STALL-SPACE', 'MARKET ELECTRIC']
        
        for source in all_sources:
            user_inputs = db.get_user_inputs(source, months=12)
            if not user_inputs.empty:
                for _, row in user_inputs.iterrows():
                    predictor.add_user_input(source, row['income_date'], row['monthly_income'])
        
        training_results = initialize_system()
        return jsonify({'success': True, 'results': str(training_results)})
    except Exception as e:
        print(f"Retrain error: {e}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/delete-income/<int:entry_id>', methods=['DELETE'])
@admin_required
def delete_income(entry_id):
    """Delete a specific income entry"""
    try:
        success = db.delete_user_income(entry_id)
        if success:
            return jsonify({'success': True, 'message': 'Entry deleted successfully'})
        else:
            return jsonify({'success': False, 'error': 'Entry not found'}), 404
    except Exception as e:
        print(f"Delete error: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Set MEEDO_DEV=1 for auto-reload during development
    dev_mode = os.environ.get('MEEDO_DEV', '').strip() in ('1', 'true', 'True', 'yes', 'YES')
    app.debug = dev_mode

    # Start server immediately; initialize models in background.
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        training_results = {}
        t = threading.Thread(target=_init_system_background, daemon=True)
        t.start()
    else:
        training_results = {}
    
    app.run(debug=dev_mode, use_reloader=dev_mode, host='0.0.0.0', port=3000)
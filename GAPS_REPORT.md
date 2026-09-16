# Gaps Report — TMT Codebase Audit

A comprehensive audit of the TMT codebase across six categories of gaps.
Findings are ordered by severity within each category.

**Audit date:** 2026-09-16
**Test suite status:** 483 tests pass (`pytest -q`, 16.65s, fully offline)
**Pyflakes status:** 31 issues across `app/` and `tests/` (0 currently caught by CI)
**Source modules:** 70 Python files in `app/`
**Test files:** 20 dedicated `test_*.py` files (+ `conftest.py`, `fakes.py`)

---

## 1. CI/CD Pipeline Gaps

The two workflows (`.github/workflows/opencode.yml`, `.github/workflows/orchestrator.yml`)
run `pytest` but perform **zero** static analysis. Code quality gates defined in
RULES §8 ("no dead code, no duplicated business logic, type hints everywhere") are
simply not enforced.

| Gap | Detail |
|---|---|
| **No linting step in CI** | Neither workflow runs `ruff`, `pyflakes`, `flake8`, or any linter. Pyflakes alone would catch all 31 current issues before they reach review. |
| **No type-checking step** | No `mypy` or `pyright` run in CI. No `[tool.mypy]` config in `pyproject.toml`, no `mypy.ini`. RULES §8 requires type hints but nothing enforces them. |
| **No ruff config exists** | `.gitignore` references `.ruff_cache/`, implying ruff was intended but no `ruff.toml` or `[tool.ruff]` section was ever created — so even running ruff locally would use defaults with no project configuration. |
| **No pre-commit hooks** | No `.pre-commit-config.yaml`. No git hooks to catch issues at commit time. |
| **No code coverage gate** | Tests pass but there is no `--cov` threshold or reporting. Coverage could drop to 0% unnoticed. |
| **No Makefile / scripts/** | No `Makefile`, no `scripts/` directory. Developers must memorize `pytest -q` and `pip install -e .[dev]`. |

### Workflow observations

- **`opencode.yml`** — triggers on `opencode.yml` changes, runs a minimal echo. Does not run tests or lint at all.
- **`orchestrator.yml`** — the more substantive workflow; runs `pip install -e .[dev]`, `pytest -q` (483 pass), `python -c "import app"`. Good on imports and tests, but no lint or type gate.

---

## 2. Code Quality Issues (Pyflakes)

**31 total issues** (15 in `app/`, 16 in `tests/`). All reproducible with
`python -m pyflakes app/ tests/`.

### app/ — 15 issues

| File | Line | Issue |
|---|---|---|
| `app/main.py` | 11 | `asyncio` imported but unused |
| `app/core/account_service.py` | 10 | `asyncio` imported but unused |
| `app/bot/reporter.py` | 13 | `aiogram.Bot` imported but unused |
| `app/bot/texts.py` | **179** | **undefined name `Config`** — used as type annotation `Config \| None` in `render_settings()` signature but never imported. Only works because `from __future__ import annotations` turns it into a string. Breaks `typing.get_type_hints()`. |
| `app/bot/texts.py` | 490 | `f"🔰\|مسار عمليه النقل ↼"` — f-string with no placeholders |
| `app/bot/texts.py` | 514 | `f"✨\|العمليه تمت ."` — f-string with no placeholders |
| `app/bot/routers/accounts.py` | 45 | `AccountStatus` imported but unused |
| `app/bot/routers/jobs.py` | 14 | `M_DELETED_ACCOUNT` imported but unused |
| `app/bot/routers/jobs.py` | 14 | `PARSE_MODE` imported but unused |
| `app/bot/routers/admin/stats.py` | 9 | `PARSE_MODE` imported but unused |
| `app/bot/routers/admin/keyboards.py` | 151 | `f"  all"` — f-string with no placeholders |
| `app/bot/routers/admin/keyboards.py` | 152 | `f"✓ active"` — f-string with no placeholders |
| `app/bot/routers/admin/keyboards.py` | 153 | `f"✓ inactive"` — f-string with no placeholders |
| `app/bot/routers/admin/keyboards.py` | 154 | `f"✓ blocked"` — f-string with no placeholders |
| `app/tg/errors.py` | 258 | `technical` variable assigned (`f"{type(exc).__name__}: {exc}"`) but never used |

### tests/ — 16 issues

| File | Line | Issue |
|---|---|---|
| `tests/conftest.py` | 17 | `CopyMessage` imported but unused |
| `tests/conftest.py` | 17 | `SendMessage` imported but unused |
| `tests/test_bcast_ab.py` | 7 | `pytest` imported but unused |
| `tests/test_bcast_personalization.py` | 9 | `pytest` imported but unused |
| `tests/test_bcast_personalization.py` | 13 | `BroadcastStatus` imported but unused |
| `tests/test_bcast_scheduling.py` | 9 | `pytest` imported but unused |
| `tests/test_broadcaster.py` | 23 | `count_audience` imported but unused |
| `tests/test_broadcaster.py` | 23 | `resolve_audience` imported but unused |
| `tests/test_broadcaster.py` | 30 | `RecipientStatus` imported but unused |
| `tests/test_broadcaster.py` | 305 | `original_copy` assigned but never used |
| `tests/test_broadcaster.py` | 379 | `original_copy` assigned but never used |
| `tests/test_broadcast_db.py` | 471 | `future` assigned but never used |
| `tests/test_broadcast_final.py` | 41 | `campaign` assigned but never used |
| `tests/test_dispatcher.py` | 327 | `SystemEvent` imported but unused |
| `tests/test_notifications.py` | 129 | `n1` assigned but never used |
| `tests/test_texts.py` | 7 | `pytest` imported but unused |

### Logic bugs found during review (not flagged by pyflakes)

| File | Line | Issue |
|---|---|---|
| `app/tg/errors.py` | 257 | `LoginFailure("PASSWORD_REQUIRED", login_message("CODE_EMPTY"))` — the `PASSWORD_REQUIRED` failure uses the **wrong message key** (`CODE_EMPTY`). Users entering a 2FA password that fails will see the "Enter the code" message instead. |
| `app/bot/routers/admin/keyboards.py` | 151 | `f"✓ {filters.target}"` when target is `"all"`, else `f"  all"` — the else branch should say `"✓ all"` (currently shows blank space + "all" with no checkmark), breaking the visual consistency of the target filter UI. |

---

## 3. Test Coverage Gaps

**483 tests pass** but cover only **~20 of 70 source modules**. The following
23 non-trivial modules have **zero** dedicated test files:

| Module | Lines | Test File | Risk |
|---|---|---|---|
| `app/config.py` | — | (none) | Configuration parsing, env var handling |
| `app/main.py` | — | (none) | Entry point, bot wiring, shutdown |
| `app/__main__.py` | — | (none) | Module entry point |
| `app/bot/__init__.py` | — | (partial: test_dispatcher.py) | `build_dispatcher` — only indirectly tested |
| `app/bot/gate.py` | 42 | (none) | **Mandatory-subscription membership check** — fail-closed security logic |
| `app/bot/keyboards.py` | 185 | (none) | All inline keyboard builders |
| `app/bot/states.py` | 23 | (none) | FSM state groups |
| `app/bot/routers/accounts.py` | — | (none) | Account CRUD handlers |
| `app/bot/routers/common.py` | 156 | (none) | `/start`, `/help`, `/cancel`, main menu, gate verify |
| `app/bot/routers/jobs.py` | — | (none) | Job list/view/cancel handlers |
| `app/bot/routers/settings.py` | — | (none) | User settings router |
| `app/bot/routers/transfers.py` | — | (none) | Transfer wizard handlers |
| `app/bot/routers/admin/backups.py` | 16 | (none) | Backup management (stub) |
| `app/bot/routers/admin/channels.py` | **221** | (none) | **Mandatory subscription CRUD** (full implementation) |
| `app/bot/routers/admin/filters.py` | — | (none) | `IsAdmin` filter |
| `app/bot/routers/admin/keyboards.py` | 264 | (none) | All admin keyboard builders |
| `app/bot/routers/admin/menu.py` | 54 | (none) | Admin `/admin` entry, menu, close |
| `app/bot/routers/admin/router.py` | 21 | (none) | Admin router assembly |
| `app/bot/routers/admin/search.py` | 16 | (none) | Search (stub) |
| `app/bot/routers/admin/settings.py` | 17 | (none) | Settings (stub) |
| `app/bot/routers/admin/stats.py` | 34 | (none) | Statistics screen |
| `app/bot/routers/admin/users.py` | **281** | (none) | **User management CRUD** (full implementation) |
| `app/services/helpers.py` | 36 | (none) | `safe_edit`, `safe_delete` — error-suppression utilities |

### Highlights

- **Entire admin router tree is untested.** `channels.py` (221 lines of full CRUD
  with FSM states, Telegram API calls, and owner-scoped repo calls) and
  `users.py` (281 lines of user lookup, block/unblock, message-to-user, and
  account deletion) are the **largest and most security-sensitive handlers**
  in the bot — and have zero tests.

- **`gate.py` untested.** The mandatory-subscription check that enforces
  admin-gate membership is a 42-line security boundary with fail-closed logic.

- **`safe_edit`/`safe_delete` untested.** Error-suppression utilities used
  across every callback handler — if they silently fail, no UI path is covered.

- **`config.py` untested.** Pydantic-settings configuration with env parsing
  is the foundation everything depends on, yet has no validation tests.

### What IS tested well

- `app/core/broadcast.py` (934 lines) — well covered by `test_broadcaster.py`,
  `test_bcast_*.py`, `test_broadcast_db.py`
- `app/db/repositories.py` (964 lines) — covered by `test_repositories.py`
- `app/tg/transfer.py` — covered by `test_transfer_engine.py`
- `app/tg/errors.py` — covered by `test_errors.py`

---

## 4. Security Vulnerabilities

### 4.1 IDOR: Notification mutation functions lack `owner_id` scoping

**Severity: High** — RULES §4 violation: "Every DB query that reads or writes
user-owned rows must be scoped by `owner_id`."

Three functions in `app/db/repositories.py` accept only `notification_id` with
no `owner_id` parameter:

```python
# repositories.py:898 — no owner_id check
async def mark_notification_read(db, notification_id: int) -> bool:
    cur = await db.conn.execute(
        "UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
        (now_iso(), notification_id),  # ← anyone can mark any notification read
    )

# repositories.py:918 — no owner_id check
async def dismiss_notification(db, notification_id: int) -> bool:
    cur = await db.conn.execute(
        "UPDATE notifications SET dismissed=1 ... WHERE id=? AND dismissed=0",
        (now_iso(), notification_id),  # ← anyone can dismiss any notification
    )

# repositories.py:928 — no owner_id check
async def delete_notification(db, notification_id: int) -> bool:
    cur = await db.conn.execute(
        "DELETE FROM notifications WHERE id=?",  # ← anyone can delete any notification
        (notification_id,)
    )
```

**Contrast** with the read-side functions that DO scope by owner_id:
- `list_notifications` (line 859): `WHERE owner_id=? ...`
- `count_unread_notifications` (line 888): `WHERE owner_id=? ...`
- `mark_all_notifications_read` (line 908): `WHERE owner_id=? ...`

**Attack scenario:** Notification IDs are sequential integers starting at 1.
An admin whose own notifications are exhausted can enumerate and mark/dismiss/delete
notifications belonging to **other admins**. The callers are in
`app/bot/routers/admin/notifications.py` — `cb_notify_read` (line 77) calls
`mark_notification_read`, and `cb_notify_dismiss` (line 98) calls `dismiss_notification`.
Note that `users.py` contains **zero** notification references. The third mutation,
`delete_notification`, has **no caller anywhere** in the codebase (only its definition
in `repositories.py:928`) — it is a **latent IDOR**: the function exists with the
vulnerability but is not currently wired into any UI path. Verify that the callers
that do exist pass the correct `owner_id` and that the repo functions enforce it.

### 4.2 Undeclared dependency: `python-dateutil`

**Severity: Medium** — RULES §8 §3: "No new dependency without: (a) a real
need, (b) a PRD note, (c) pin update in `pyproject.toml`."

`app/bot/routers/admin/broadcast.py:128` does:
```python
from dateutil import parser as du_parser
```
`python-dateutil` is **not** listed in `pyproject.toml` dependencies.
Running `pip install -e .[dev]` will install it transitively or fail at runtime
when an admin uses natural-language scheduling.

### 4.3 Broad `except Exception` across the codebase

**Severity: Medium** — RULES §2: "New error classes ... never `except Exception: pass`,
never bare `except:`."

38 occurrences across 17 source files. Highlights:

| File | Lines | Context | Defensible? |
|---|---|---|---|
| `app/services/helpers.py` | 22, 25, 35 | `safe_edit`, `safe_delete` — error suppression with no logging | **No** — swallows Telegram API errors silently |
| `app/core/broadcast.py` | 497, 502, 583, 607, 746, 769, 784, 801, 814, 839 | Worker loop, progress card, send retry | Mixed — some should use specific Telethon/aiogram exceptions |
| `app/bot/routers/admin/channels.py` | 130 | `entry_ref_entered` — catches all exceptions from `bot.get_chat` | **No** — masks auth failures as "not found" |
| `app/core/account_service.py` | 150 | Account validation | Mixed |
| `app/bot/reporter.py` | 108, 132 | Bot notification dispatch | Mixed |
| `app/core/notifications.py` | 125 | Event publishing | Defensible (best-effort publish) |
| `app/__main__.py` | 32 | Shutdown cleanup | Defensible |
| `app/bot/gate.py` | 33 | `check_membership` — fail-closed | Defensible (intentional fail-closed) |
| `app/bot/middlewares.py` | 156 | User blocking gate | Mixed |
| `app/config.py` | 83 | Pydantic validation | Defensible (catches ValidationError) |

The `safe_edit`/`safe_delete` pair in `services/helpers.py` is the most concerning:
they catch **all** exceptions and silently `pass` (no logging), meaning any
Telegram API error during UI updates is invisible. RULES §3 says "never lose
data silently."

### 4.4 Emoji/symbol whitelist in tests contradicts RULES §7

`tests/test_texts.py:26` defines `_ALLOWED_SYMBOLS` that **whitelists emoji**
and custom Unicode symbols. RULES §7 explicitly forbids: "emoji, ASCII art,
box-drawing, text tables, space-aligned layouts." The test encodes the opposite
policy, making it a **test that enforces the wrong rule**.

---

## 5. Technical Debt & Dead Code

### 5.1 Duplicated `now_iso()` function

```python
# app/db/repositories.py:25 — primary definition
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# app/core/broadcast.py:932 — exact copy
def now_iso() -> str:
    """UTC timestamp in ISO-8601 'Z' form (mirrors repositories.now_iso)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

The docstring explicitly acknowledges the duplication ("mirrors repositories.now_iso").
RULES §8: "no duplicated business logic. If two modules need the same rule, lift
it to `core/` or `tg/errors.py`." This should be a single shared utility.

### 5.2 Test-covered but not wired into production: `render_bcast_progress`

Defined at `app/bot/texts.py:648` and listed in `__all__` (line 50). Contrary to
the original report, this function is **not dead code** — it is called by the test
`test_render_bcast_progress` in `tests/test_broadcast_router.py:310` and is live
and test-covered. However, it is **not wired into the production broadcast path**:
the comment at `app/core/broadcast.py:206` says:

> "Phase 4 replaces this with a full `render_bcast_progress` in `texts.py`."

But the replacement was never wired up — `broadcast.py` still uses `_progress_text()`
(line 202, called at line 825). The Phase 4 refactor is **half-done**: the new
function exists and is tested, but the old `_progress_text()` remains in active
use. Recommended action: route the production call through `render_bcast_progress`
(or remove it if `_progress_text()` is preferred) so the two do not diverge.

### 5.3 Test-covered but not wired into production: `render_notification_card`

Defined at `app/bot/texts.py:738` and listed in `__all__` (line 53). Contrary to
the original report, this function is **not dead code** — it is called by the test
`test_render_notification_card` in `tests/test_texts.py:349` and is live and
test-covered. However, it is **not wired into the production notification UI path**:
the notification inbox uses `render_notifications_list` instead
(`app/bot/texts.py`, called from `admin/notifications.py:47`). Recommended action:
either wire `render_notification_card` into the production single-notification view
or remove it (and its test) to avoid bitrot.

### 5.4 Dead variable: `technical` in `errors.py`

Line 258: `technical = f"{type(exc).__name__}: {exc}"` is computed (presumably
for error logging) but never used. The `LoginFailure("UNEXPECTED", ...)` on
line 259 discards this information. Either log it or remove it.

### 5.5 Admin stubs: three routers are placeholders

| File | Lines | Content |
|---|---|---|
| `app/bot/routers/admin/backups.py` | 16 | `cb_backups` just sends "💾\|إدارة النسخ الاحتياطية" — no backup logic |
| `app/bot/routers/admin/search.py` | 16 | `cb_search` just sends "🔎 أرسل معرف المستخدم..." — no search logic |
| `app/bot/routers/admin/settings.py` | 17 | `cb_settings` just sends "⚙️ الإعدادات" — no settings logic |

These are registered in `router.py:21` and appear in the admin menu, but
implement no actual functionality.

### 5.6 Empty `i18n` module

`app/i18n/__init__.py` is **0 bytes** — a completely empty module with no
implementation. The `__init__.py` at line 1-6 of `texts.py` imports from it...
actually, let me verify: no, `texts.py` does not import from `app.i18n`. The
module is simply a dead placeholder.

### 5.7 Wrong message key (logic bug)

`app/tg/errors.py:257`:
```python
return LoginFailure("PASSWORD_REQUIRED", login_message("CODE_EMPTY"))
```

The `PASSWORD_REQUIRED` key gets the `CODE_EMPTY` message. Should be
`login_message("PASSWORD_REQUIRED")`.

---

## 6. Documentation Gaps

### 6.1 README omits the entire admin panel

The README (`README.md`, 142 lines) documents the **user-facing** transfer flow
(Accounts → Add account → Transfer → Preflight → Confirm) but says **nothing**
about:

- The `/admin` command
- Users management (block/unblock, message-to-user)
- Broadcast campaigns
- Mandatory subscription gate (channels/groups management)
- Statistics screen
- Search
- Backups
- Settings

The admin panel is the most complex feature surface in the bot (see
`app/bot/routers/admin/` — 264 lines of keyboards, 281 lines of user CRUD,
221 lines of channel CRUD), yet it is **completely undocumented** in the README.

### 6.2 No emoji policy is documented in RULES

RULES §7 states: "Forbidden: emoji, ASCII art, box-drawing, text tables,
space-aligned layouts." But 16 source files currently **violate** this rule.
There is no migration plan, no lint rule, and no test that enforces it.
Meanwhile `tests/test_texts.py:26` actively **whitelists emoji**, directly
contradicting the rule.

### 6.3 Missing linting and tooling config

| Expected file | Exists? |
|---|---|
| `ruff.toml` / `[tool.ruff]` in pyproject | No |
| `.flake8` | No |
| `mypy.ini` / `[tool.mypy]` in pyproject | No |
| `.pre-commit-config.yaml` | No |
| `setup.py` | No (pyproject.toml handles build — acceptable) |
| `requirements.txt` | No (pyproject.toml handles deps — acceptable) |
| `Makefile` | No |

`.gitignore` line reference to `.ruff_cache` implies ruff was planned but
never configured — developers who install ruff locally get zero guidance.

### 6.4 Undeclared dependency

As noted in §4.2, `python-dateutil` is used in `app/bot/routers/admin/broadcast.py:128`
but not declared in `pyproject.toml`. This is both a tooling gap (no CI check
for undeclared imports) and a runtime risk.

### 6.5 Inconsistent file headers

`docs/broadcast/BroadcastEngine.md` references "Phase 3" and "Phase 4" progress
markers inline in source code comments (e.g., `broadcast.py:200`:
`# Phase 3 placeholder`), but these phase references are not tracked as
formal issue markers (no TODO comments anywhere in `app/`). A grep for
`TODO|FIXME|HACK|XXX` in `app/` returns **zero results** — progress markers
are embedded in prose comments, not actionable issue references.

---

## Summary

| Category | Count | Severity |
|---|---|---|
| CI/CD pipeline gaps | 6 | High |
| Code quality (pyflakes) | 31 | Medium-High |
| Test coverage gaps | 23 modules untested | High |
| Security vulnerabilities | 4 | High-Medium |
| Technical debt / dead code | 7 | Medium |
| Documentation gaps | 5 | Medium |

**Immediate priorities:**

1. **Add lint + type-check to CI** — catches 31 pyflakes issues automatically
2. **Fix IDOR in notification mutations** — add `owner_id` parameter to
   `mark_notification_read`, `dismiss_notification`, `delete_notification`
3. **Fix the `Config` undefined name** in `texts.py:179` — breaks type introspection
4. **Remove dead code** — the duplicated `now_iso` and the `technical` variable
   in `errors.py`
5. **Resolve half-wired functions** — `render_bcast_progress` and
   `render_notification_card` are test-covered but not wired into production
   (either complete the wiring or remove them to prevent bitrot)
6. **Add `python-dateutil` to `pyproject.toml`** — prevents runtime crash
7. **Fix the wrong message key** in `errors.py:257` — user-facing correctness
8. **Resolve the emoji policy contradiction** — either strip all emoji from
   16 files or update RULES §7 with an approved palette
8. **Resolve the emoji policy contradiction** — either strip all emoji from
   16 files or update RULES §7 with an approved palette

> *Note: This report is a gap analysis only. No source code was modified.*

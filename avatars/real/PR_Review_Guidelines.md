# PR Review Guidelines
**A Rulebook for Maintaining Code Quality in Pull Requests**
_Python Codebase · Version 2.0 · March 2026_

---

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Review Process Overview](#2-review-process-overview)
3. [Code Quality Standards](#3-code-quality-standards)
4. [Error Handling & Resilience](#4-error-handling--resilience)
5. [Security Checklist](#5-security-checklist)
6. [Performance Considerations](#6-performance-considerations)
7. [Testing Requirements](#7-testing-requirements)
8. [Documentation & Comments](#8-documentation--comments)
9. [Review Etiquette & Communication](#9-review-etiquette--communication)
10. [Comment Severity Levels](#10-comment-severity-levels)
11. [Quick Checklists](#11-quick-checklists)
12. [Exceptions & Escalation](#12-exceptions--escalation)

---

## 1. Purpose & Scope

This document defines the standards and expectations for reviewing Pull Requests (PRs) on this team. Every reviewer and author should use this guide to ensure consistent, high-quality, maintainable code reaches the main branch.

> 📌 **Goal:** Catch bugs early, enforce standards, improve readability, and protect the codebase from technical debt — before code is merged.

**Who this applies to:**
- All engineers submitting or reviewing PRs
- Tech leads performing final approvals
- Interns and new joiners (reviewed PRs are a learning opportunity)

---

## 2. Review Process Overview

### 2.1 Before You Start Reviewing

| Check | Detail |
|---|---|
| ✅ **Understand the ticket/story** | Read the linked Jira/GitHub issue before diving into the diff |
| ✅ **Check CI/CD status** | Do not review if the pipeline is failing — ask the author to fix it first |
| ✅ **Check PR description** | Is it filled out properly? Does it explain what changed and why? |
| ✅ **Check PR size** | Large PRs (> 400 lines of logic) should be split — flag this early and ask the author to break it up |
| ✅ **Check PR template completeness** | All sections (Summary, Changes Made, Testing) must be filled in — do not review incomplete PRs |

### 2.2 Turnaround Time & Notifications

- **First review response:** within 1 business day of assignment
- **Re-review after changes:** within 4 business hours
- **Stale PRs (no activity > 3 days):** it is the **author's responsibility** to tag the reviewer in the **Teams group** and post a reminder that a PR is awaiting review
- All review reminders and nudges must go through the **Teams group — not DMs** — so the whole team has visibility
- If a reviewer is unavailable or blocked, the author should flag this in Teams so another reviewer can be assigned

### 2.3 Approval Requirements

- Minimum **1 approval** required before merging
- All review comments must be addressed (fixed or justified) and resolved by the reviewer before merging
- **Do not self-merge** — always get a second pair of eyes
- Ensure the target branch is correct (e.g. not accidentally targeting `main` instead of `develop`)

### 2.4 Resolving Review Comments

> 🚫 **Authors must NOT self-resolve review comments.** It is solely the reviewer's responsibility to mark a comment as resolved — either after verifying the fix or after reading the author's justification for not making the change.

- **If you have made the requested change:** reply with a brief note (e.g. `Fixed in commit abc123`) and wait for the reviewer to resolve it
- **If you disagree:** reply with your reasoning clearly — the reviewer then decides whether to resolve, push back, or escalate
- Self-resolving comments bypasses the review loop and defeats the purpose of the review process

### 2.5 PR Size & Scope

> 💡 Smaller PRs are reviewed faster, more thoroughly, and are easier to revert if something goes wrong. Aim for focused, single-purpose PRs.

- Keep PRs focused on a single feature, bug fix, or refactor — avoid bundling unrelated changes
- If a PR grows beyond ~400 lines of logic, discuss splitting it with your tech lead before requesting review
- Refactoring changes should be in a separate PR from feature or bug fix changes where possible
- Dependency upgrades should be in their own dedicated PR with explicit testing notes

---

## 3. Code Quality Standards

### 3.1 No Hardcoding

> 🚫 **NEVER hardcode values that may change across environments or over time.**

| Rule / Check | Guidance / Example |
|---|---|
| **Environment-specific values** | URLs, IPs, ports, hostnames must come from config files or environment variables |
| **Business constants** | Tax rates, limits, thresholds → define in a constants file or DB config |
| **Magic numbers** | Replace `86400` with `SECONDS_IN_A_DAY = 86400`, defined in one place |
| **Credentials & secrets** | API keys, passwords, tokens must NEVER appear in code — use a secrets manager or vault |
| **File paths** | Avoid absolute paths like `/Users/john/…` — use relative paths or configurable base dirs |
| **Feature flags** | Boolean toggles that control behaviour must be externally configurable, not hardcoded `True`/`False` |

### 3.2 Reusability & DRY Principle

DRY = *"Don't Repeat Yourself"*. If the same logic appears in more than one place, it must be extracted.

- ✅ **Extract repeated logic** — Create utility functions, helpers, or services for code used in 2+ places
- ✅ **Reuse existing utilities** — Check if a helper for this already exists before writing a new one
- ✅ **Generic over specific** — Prefer configurable, parameterised functions over highly specific one-offs
- ✅ **Module boundaries** — API handlers, business logic, DB access, and utilities should be clearly separated and independently reusable

### 3.3 Naming Conventions

| Rule / Check | Guidance / Example |
|---|---|
| **Variables & functions** | `snake_case`: `get_user_by_id()`, not `getUser()` or `func1()` |
| **Classes** | `PascalCase`: `UserService`, `PaymentProcessor` |
| **Boolean variables** | Prefix with `is_`, `has_`, `can_`, `should_`: `is_active`, `has_permission`, `can_delete` |
| **Constants** | `SCREAMING_SNAKE_CASE` at module level: `MAX_RETRY_COUNT`, `DEFAULT_TIMEOUT_SECONDS` |
| **Private methods/attrs** | Single underscore prefix: `_calculate_total()` — double underscore only when name mangling is explicitly needed |
| **File names** | `snake_case` consistently: `user_service.py`, `payment_processor.py` |
| **Test files/functions** | `test_` prefix required: `test_user_service.py`, `test_returns_404_when_user_not_found()` |

### 3.4 Python-Specific Quality Checks

- ✅ **Type hints on all public functions** — All function signatures must have type annotations — use `from __future__ import annotations` for forward references
- ✅ **No bare except clauses** — `except Exception as e:` is the minimum — bare `except:` catches `SystemExit` and `KeyboardInterrupt` too
- ✅ **Use dataclasses or Pydantic models** — Avoid plain dicts for structured data — use `@dataclass` or Pydantic `BaseModel` for clarity and validation
- ✅ **List/dict comprehensions over loops** — Prefer `[x for x in items if condition]` over multi-line for-loops for simple transformations
- ✅ **Context managers for resources** — Always use `with` for file I/O, DB connections, locks — never manually call `.close()`
- ✅ **f-strings over `.format()` or `%`** — Use f-strings for string formatting unless dynamic template keys are required
- ✅ **Avoid mutable default arguments** — `def func(items=[])` is a classic Python bug — use `def func(items=None)` and initialise inside the body
- ✅ **Avoid wildcard imports** — Never use `from module import *` — it pollutes the namespace and makes dependencies opaque
- ✅ **Use `__all__` for public APIs** — Modules that expose a public interface should define `__all__` to make exports explicit

### 3.5 Function & Method Quality

- ✅ **Single Responsibility** — Each function should do ONE thing — if it does more, split it
- ✅ **Function length** — Flag functions over ~40 lines — they likely need to be broken up
- ✅ **Parameter count** — More than 3–4 params → consider a config object or dataclass
- ✅ **No side effects in pure logic** — Computation logic should not trigger DB writes, API calls, or I/O
- ✅ **Return early / guard clauses** — Use early returns to reduce nesting and improve readability

---

## 4. Error Handling & Resilience

### 4.1 Required Error Handling

- ✅ **All async calls handled** — Every async function and I/O operation must handle exceptions explicitly
- ✅ **No bare except** — Never use bare `except:` — always catch a specific exception type or at minimum `Exception as e`
- ✅ **Meaningful error messages** — Errors should include context — `raise ValueError(f'Expected positive int, got {value}')`
- ✅ **No silent failures** — Avoid empty except blocks — at minimum, log the error with `logger.exception()`
- ✅ **Proper HTTP status codes** — APIs must return correct codes: `400` bad input · `401` unauthenticated · `403` forbidden · `404` not found · `500` server error
- ✅ **Custom exceptions for domain errors** — Define custom exception classes (e.g. `class InsufficientFundsError(ValueError)`) rather than raising generic exceptions
- ✅ **Retries for transient failures** — Network calls and external service calls should have retry logic with exponential backoff where appropriate

> 🚫 **RED FLAG: `except: pass`** — A silent bare except block is never acceptable. It swallows all exceptions including `KeyboardInterrupt` and hides bugs completely.

### 4.2 Logging

- Use Python's standard `logging` module — **never use `print()` statements in production code**
- Log at appropriate levels: `logger.debug()`, `logger.info()`, `logger.warning()`, `logger.error()`, `logger.exception()`
- Use `logger.exception()` inside `except` blocks — it automatically captures the full stack trace
- Never log sensitive data: passwords, tokens, PII, API keys
- Structured logs (JSON via `structlog` or similar) preferred over string concatenation
- Include enough context in log messages to debug without needing to reproduce the issue

---

## 5. Security Checklist

> 🔐 **Security issues are BLOCKING. A PR with a security vulnerability must not be merged.**

- 🔒 **No secrets in code** — API keys, tokens, credentials must never appear in source code — use environment variables or a secrets manager
- 🔒 **Input validation** — All user inputs must be validated and sanitised before use — use Pydantic or marshmallow for structured validation
- 🔒 **SQL injection prevention** — Use SQLAlchemy ORM or parameterised queries — never use `%` or `.format()` to build SQL strings from user input
- 🔒 **XSS prevention** — Django/Jinja2 auto-escapes by default — ensure it is not disabled with `|safe` or `mark_safe()` unless explicitly reviewed
- 🔒 **Auth checks on new endpoints** — Every new API endpoint must enforce appropriate authentication and authorisation — no unprotected endpoints
- 🔒 **Principle of least privilege** — Code should request only the permissions and scopes it actually needs
- 🔒 **Dependency vulnerabilities** — New packages must not introduce known CVEs — run `pip-audit` or `safety check` on `requirements.txt`
- 🔒 **Sensitive data in URLs** — Passwords, tokens, and IDs must never be passed as query parameters — use request body or headers
- 🔒 **Mass assignment protection** — Serialisers/forms must explicitly whitelist accepted fields — avoid accepting arbitrary user-supplied fields

---

## 6. Performance Considerations

### 6.1 Database & Queries

- ✅ **N+1 query check** — Loops that trigger DB queries inside them are a red flag — use batch queries, `select_related()`, or `prefetch_related()`
- ✅ **Index awareness** — New queries filtering or ordering on large tables should use indexed columns — flag if unsure
- ✅ **Pagination** — Any query that could return unbounded rows must be paginated — never fetch all records
- ✅ **Transaction scope** — Long-running transactions lock rows and block other operations — keep them as short as possible
- ✅ **Avoid loading full objects for count/exist checks** — Use `.count()` or `.exists()` instead of `len(queryset)` or `if queryset:`

### 6.2 General Performance

- ✅ **No blocking calls in async context** — Avoid synchronous I/O or heavy computation inside async functions — use `asyncio`, Celery, or background tasks
- ✅ **Generator usage** — For large datasets, prefer generators and lazy evaluation over loading everything into memory at once
- ✅ **Caching opportunities** — Flag repeated expensive computations that could be cached — consider `@lru_cache`, Redis, or Django's cache framework
- ✅ **Memory leaks** — Ensure file handles, DB connections, and resources are properly closed — always use `with` blocks

---

## 7. Testing Requirements

### 7.1 Coverage Expectations

| Change Type | Expectation |
|---|---|
| **New features** | Must include unit tests covering the happy path AND key edge cases |
| **Bug fixes** | Must include a regression test that would have caught the bug before the fix |
| **Refactoring** | Existing tests must still pass; add tests if coverage drops |
| **Utility functions** | 100% coverage expected — they are the foundation of the codebase |
| **Critical paths (payments, auth)** | Integration tests required in addition to unit tests |
| **New API endpoints** | Must have at least: success case, invalid input (400), unauthorised (401/403), not found (404) |

### 7.2 Test Quality Checklist

- ✅ **Use pytest** — All tests must use `pytest` — not `unittest` directly unless there is a strong existing reason
- ✅ **Test names are descriptive** — `test_returns_404_when_user_does_not_exist`, not `test1` or `test_func`
- ✅ **Tests are independent** — Tests must not depend on execution order or shared mutable state — use fixtures for setup and teardown
- ✅ **No testing implementation details** — Test behaviour and outputs, not internal method calls or private attributes
- ✅ **Mocks used appropriately** — External services (APIs, DBs, email) must be mocked using `unittest.mock` or `pytest-mock` in unit tests
- ✅ **Edge cases covered** — `None`/null values, empty lists/dicts, boundary values, unauthorised access, type errors
- ✅ **No hardcoded test data** — Use fixtures or factories (`factory_boy`) — avoid duplicating test setup across multiple test files
- ✅ **Assertions are specific** — Assert the exact value or exception, not just that something is truthy — `assert response.status_code == 200`, not `assert response`

---

## 8. Documentation & Comments

### 8.1 Code Comments

Comments should explain **WHY**, not **WHAT**. If code needs a comment to explain what it does, it should be refactored to be self-explanatory instead.

```python
# ✅ Good
# Retry up to 3 times — third-party payment API has known intermittent failures
response = call_with_retry(payment_api.charge, retries=3)

# 🚫 Bad
# Increment counter by 1
counter += 1
```

- ✅ **Docstrings on all public functions and classes** — Use Google-style or NumPy-style docstrings — include `Args`, `Returns`, and `Raises` sections
- ✅ **TODO comments include owner and ticket** — `# TODO(john): Remove after migration completes — PROJ-1234`
- ✅ **No commented-out code** — Dead code must be deleted, not commented out — git history exists for a reason
- ✅ **Complex algorithms explained** — If the logic is inherently non-obvious, add a brief comment explaining the approach

### 8.2 PR Description & Template

Every PR must use the team PR template and fully fill out all sections:

- **Title:** Clear and action-based (e.g. `Add rate limiting to /payments endpoint`)
- **Summary:** What was changed and why
- **Linked Issues:** Jira/GitHub ticket reference
- **Changes Made:** Key changes listed clearly
- **Testing:** How was this tested — unit tests, manual steps, screenshots if relevant
- **Breaking Changes:** Any API, DB schema, or config changes that break existing behaviour
- **Deployment Notes:** Env variable changes, migration runs, or specific deployment order
- **Notes:** Known limitations, follow-up tickets, side effects

> 🚫 **Incomplete PR descriptions will be sent back to the author before review begins.** Reviewers are not expected to infer context that should be in the description.

### 8.3 Changelog & Migration Notes

- ✅ **Breaking changes documented** — Any change that breaks an existing API contract, DB schema, or config format must be clearly called out in the PR description
- ✅ **Migration scripts included** — DB migrations must be included in the **same PR** as the model change — never merged separately
- ✅ **Deployment notes** — If the PR requires env variable changes, config updates, or a specific deployment order, this must be noted in the PR

---

## 9. Review Etiquette & Communication

### 9.1 Giving Feedback

| Rule | Guidance |
|---|---|
| **Be specific** | Don't say "this is messy". Say "this function is doing 3 things — consider splitting into `get_user()`, `validate_permissions()`, and `send_notification()`" |
| **Explain the why** | Link to docs, team standards, or explain the reasoning — reviewers should teach, not just point out issues |
| **Distinguish severity** | Use comment prefixes (see Section 10) so the author knows what is blocking vs. a suggestion |
| **Be kind** | Review the code, not the person — "We usually prefer..." is better than "You should have..." |
| **Ask questions first** | Prefer "What was the thinking here?" over "This is wrong" — there may be context you're missing |
| **Acknowledge good work** | If a solution is clever or well-done, say so — positive reinforcement matters |

### 9.2 Responding to Feedback (Author)

- ✅ **Respond to every comment** — Either fix it, explain why you disagree, or acknowledge it — never leave comments hanging
- ✅ **Reply before re-requesting review** — Address all open comments and leave replies before tagging the reviewer again
- ✅ **Don't silently dismiss** — If you push back on a comment, explain your reasoning clearly — start a discussion
- ✅ **Re-request review via Teams** — After addressing all feedback, tag the reviewer in the Teams group to let them know the PR is ready for another pass
- ✅ **Do not self-resolve comments** — Only the reviewer resolves their own comments — see [Section 2.4](#24-resolving-review-comments)

---

## 10. Comment Severity Levels

All reviewers must prefix their comments with one of the following labels so the author knows the urgency and expected action:

| Label | When to use |
|---|---|
| 🚨 **BLOCKING** | Must be fixed before merge. Security issues, data integrity bugs, crashes, incorrect logic, missing tests on critical paths. |
| ⚠️ **SHOULD FIX** | Strong recommendation. Significant technical debt, missing error handling, notable performance issue, unclear naming. |
| 💡 **SUGGESTION** | Non-blocking improvement. Readability, minor refactoring, an alternate approach worth considering. |
| **nit:** | Tiny style/preference issue. Reviewer is fine if it stays as-is — purely cosmetic. |
| ❓ **QUESTION** | A genuine question to understand the intent — not a request to change anything unless the answer reveals a problem. |
| 📚 **LEARNING** | Educational comment sharing a better approach or useful context — no action required, purely informational. |

> 💡 If a PR has only `nit:` and 💡 comments, the reviewer should **approve** and leave it to the author's discretion to address them.

---

## 11. Quick Checklists

### 11.1 Author Checklist — Before Requesting Review

- [ ] PR title is clear and action-based
- [ ] PR template fully filled out (Summary, Changes Made, Testing, Notes)
- [ ] Linked to the correct Jira/GitHub ticket
- [ ] Target branch is correct
- [ ] No hardcoded values, secrets, or magic numbers
- [ ] No duplicate logic — DRY principle followed
- [ ] All public functions have type hints and docstrings
- [ ] No bare `except:` or `except: pass` blocks
- [ ] No `print()` statements — using `logger` with correct log levels
- [ ] No sensitive data in logs or URLs
- [ ] Input validation present for all user-facing inputs
- [ ] Tests written using `pytest` — happy path + edge cases + error cases
- [ ] No commented-out code or debug statements left in
- [ ] CI/CD pipeline is green
- [ ] Reviewer tagged in the Teams group after raising the PR

### 11.2 Reviewer Checklist — Before Approving

- [ ] CI/CD pipeline is green before starting review
- [ ] PR description is complete and matches the diff
- [ ] Code logic is correct and matches the linked ticket
- [ ] No hardcoded values, magic numbers, or secrets present
- [ ] No duplicate logic — existing utilities reused where applicable
- [ ] Functions are well-named, typed, and single-responsibility
- [ ] No bare `except:` clauses or silent error swallowing
- [ ] No `print()` or debug statements left in
- [ ] New endpoints have proper authentication and authorisation
- [ ] SQL queries use ORM or parameterised queries
- [ ] Tests are present, use `pytest`, and cover edge cases
- [ ] No performance red flags (N+1, missing pagination, unbounded queries)
- [ ] Breaking changes, migrations, and deployment notes are documented
- [ ] All comments resolved by **me (the reviewer)** — not self-resolved by the author

---

## 12. Exceptions & Escalation

- If you disagree with a reviewer's blocking comment and cannot resolve between yourselves, **escalate to the Tech Lead**
- Emergency hotfixes may be merged with 1 approval, but must be followed up with a clean PR within **24 hours** addressing any shortcuts taken
- Legacy code that pre-dates these guidelines should be flagged with a comment but does not need to be fully refactored in the same PR — create a follow-up ticket
- If a PR depends on another in-flight PR, this dependency must be clearly stated in the PR description and the reviewer must be informed

> 📣 **These guidelines are a living document.** If you feel a rule is unclear, missing, or needs updating, raise it in the team's next engineering sync.

## Title
<!-- Clear and action-based title (e.g., Add rate limiting to /payments endpoint) -->

## Summary
<!-- Brief explanation of what this PR does and why it's needed -->

## Linked Issues/Tickets
<!-- Reference related issues or tickets -->

Related to #

## Type of Change
<!-- Check all that apply -->

- [ ] Bug fix
- [ ] New feature
- [ ] Refactoring (no functional changes)
- [ ] Performance improvement
- [ ] Dependency upgrade
- [ ] Configuration / infra change
- [ ] Documentation update

## Changes Made
<!-- List the key changes in this PR -->

-

-

-

## Testing
<!-- Describe how the changes were tested and add screenshots if necessary -->

-

## Breaking Changes
<!-- Does this PR introduce any breaking changes to APIs, DB schema, or configs? If yes, describe the impact and migration path. -->

- [ ] No breaking changes
- [ ] Yes — described below:

## Deployment Notes
<!-- Does this PR require env variable changes, config updates, migration runs, or a specific deployment order? -->

- [ ] No special deployment steps
- [ ] Yes — described below:

## Notes (Optional)
<!-- Mention any known limitations, TODOs, follow-up tickets, or important context -->

-

---

## Author Checklist
<!-- Complete this before requesting a review -->

- [ ] PR title is clear and action-based
- [ ] All sections above are filled out
- [ ] Linked to the correct Jira/GitHub ticket
- [ ] Target branch is correct
- [ ] No hardcoded values, secrets, or magic numbers (use env vars / config)
- [ ] No duplicate logic — DRY principle followed
- [ ] All public functions have type hints and docstrings
- [ ] No bare `except: pass` — all exceptions handled with meaningful messages
- [ ] No `print()` statements — using `logger` with appropriate log levels
- [ ] No sensitive data (passwords, tokens, PII) in logs, URLs, or code
- [ ] Input validation present for all user-facing inputs
- [ ] Tests written using `pytest` — happy path + edge cases + error cases
- [ ] No commented-out code or debug statements left in
- [ ] CI/CD pipeline is green before requesting review
- [ ] Reviewer tagged in the Teams group after raising the PR

---

## Reviewer Checklist
<!-- Complete this before approving -->

- [ ] CI/CD pipeline is green before starting review
- [ ] PR description is complete and matches the diff
- [ ] Code logic is correct and matches the linked ticket
- [ ] No hardcoded values, magic numbers, or secrets
- [ ] No duplicate logic — existing utilities reused where applicable
- [ ] Functions are well-named, typed, and single-responsibility
- [ ] No bare `except:` clauses or silent error swallowing
- [ ] No `print()` or debug statements left in
- [ ] New endpoints have proper authentication and authorisation checks
- [ ] SQL queries use ORM or parameterised queries (no raw string formatting)
- [ ] Tests are present, use `pytest`, and cover edge cases
- [ ] No performance red flags (N+1 queries, missing pagination, unbounded results)
- [ ] Breaking changes, migrations, and deployment notes are documented
- [ ] All comments resolved by **me (the reviewer)** — not self-resolved by the author

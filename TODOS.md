# TODOS

## Rebalance the screen

- **What:** define and build a "rebalance" action for iTerm2 windows.
- **Why:** windows pile up after a day of work. User, 2026-09-18: "I might want to
  rebalance the screen."
- **Pros:** a fast tidy-up that does not move tabs (the user moves tabs by hand).
- **Cons:** the meaning is not defined yet (equal sizes? a grid? move windows off a
  crowded display?); risk of moving windows placed on purpose.
- **Context:** deferred in the design review of
  `~/.claude/plans/2026-09-18-ccwho-find-your-sessions.md` (D11/D12). Auto layouts
  (by-project, triage) were dropped in the same decision.
- **Depends on:** the placement reader from that plan's Release 2 (R2.3, window bounds by tty).

## Which account each session uses, and how much subscription is left

- **What:** show the login (email / plan) per session, and the 5-hour / 7-day usage left.
- **Why:** user, 2026-09-18: "I want to see which account is being used (which login),
  but without leaking creds, and how much is left on the sub."
- **Pros:** know which account a session burns, and when a limit is close.
- **Cons:** no clean source found yet.
- **Context:** dropped from v1 in the devex review (cross-model tension T3). The user
  switches accounts with a `CLAUDE_OAUTH_*` token in the session's environment, so
  `~/.claude.json` → `oauthAccount` and config folders give the wrong account. Reading
  another process's environment (`ps eww`) returns every variable, secrets included, so
  it breaks the no-credentials rule. Usage: Claude Code gives `rate_limits.five_hour` /
  `seven_day` only to a statusLine command (docs: code.claude.com/docs/en/statusline);
  known issues: numbers drift between terminals (anthropics/claude-code#75408) and the
  model-specific weekly limit is missing (#91920). The OAuth usage endpoint needs the
  keychain token — excluded.
- **Possible way:** a `ccwho statusline` command runs *inside* each session, so it sees
  that session's own environment legitimately; it could record which account label is in
  use (never the token) plus `rate_limits`, keyed by session id. Needs: a way to map a
  token to a label without storing the token, and the concurrency rules Codex listed
  (receipt time vs measurement time, 15 concurrent writers).
- **Depends on:** Release 2's settings.json write rules (the statusLine is a shared
  setting).

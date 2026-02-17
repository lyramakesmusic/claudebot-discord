# Wishlist

## /usage command (slash command)
Show Claude Code and Codex usage stats in Discord.

### Claude Code (Anthropic)
Undocumented response headers on `/v1/messages` with OAuth token.

**Auth file:** `~/.claude/.credentials.json`
- Token at: `.claudeAiOauth.accessToken` (format: `sk-ant-oat01-...`)
- Subscription info: `.claudeAiOauth.subscriptionType` (e.g. "max"), `.rateLimitTier`

**Headers returned on any API call:**
- `anthropic-ratelimit-unified-5h-utilization` — % used in 5h window
- `anthropic-ratelimit-unified-7d-utilization` — % used in 7d window
- `anthropic-ratelimit-unified-5h-reset` — ISO 8601 reset time
- `anthropic-ratelimit-unified-7d-reset` — ISO 8601 reset time

**To fetch:** POST `https://api.anthropic.com/v1/messages` with a tiny haiku call (~$0.001).
Auth header: `Authorization: Bearer <access_token>`, plus `anthropic-version: 2023-06-01`.
Existing CLI tool: `npx claude-rate-monitor --json` (but spawns node — slow).

### Codex (OpenAI)
Undocumented endpoint — free read-only GET.

**Auth file:** `~/.codex/auth.json`
- Token at: `.tokens.access_token` (JWT)
- Account ID at: `.tokens.account_id` (UUID, goes in header)

**Endpoint:** `GET https://chatgpt.com/backend-api/wham/usage`
- Header: `Authorization: Bearer <access_token>`
- Header: `ChatGPT-Account-Id: <account_id>`

**Response shape:**
```json
{
  "primary_window": {
    "used_percent": 42.5,
    "reset_after_seconds": 12345
  },
  "secondary_window": {
    "used_percent": 15.0,
    "reset_after_seconds": 98765
  }
}
```
`primary_window` = 5hr rolling, `secondary_window` = weekly.

### Implementation notes
- Both APIs are undocumented and could break at any time
- Claude token may need refresh (check `expiresAt` in credentials)
- Codex token refreshes automatically via `last_refresh` + `refresh_token`
- Discord embed with side-by-side bars would look nice
- Could also add to bot status line or periodic auto-post

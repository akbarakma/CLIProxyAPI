# Usage readings

The service reads provider credentials on the proxy host and returns quota data only.
Run its tests with `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s usage -v`.

## Claude

OAuth accounts read `/api/oauth/usage?cedar_ember=1&skip_spend=1` with Claude CLI headers.
The service reads Fable from the scoped weekly meter in `limits`.
An absent Fable meter produces no Fable window.

Setup tokens use one Fable request with a one-token output limit.
Its response headers report Session, Weekly, and Fable usage.
Quota headers remain usable when the request returns HTTP 429.
This request spends quota and can open a session, so setup accounts remain manual.

An account can set `fable_included_until` to an ISO timestamp in the account configuration.
Its Fable window carries that timestamp as optional `expiresAt`, in epoch milliseconds.
The collector omits the window after expiry.
Clients must also hide expired windows from cached readings.
Accounts without this setting have no promotional expiry.
No scheduled cleanup is needed.

The optional `resetGrants` object contains `grants` and `fetchedAt`.
Each grant reports its ID, label, remaining count, start, expiry, pause state, and current usability.
Fable grants are identified by the usage they restore, because their upstream type can also be `full`.
Clients exclude expired and future grants from current counts and retain their expiry details.
Paused grants remain held but cannot be used.
An empty inventory means zero resets; an absent inventory means unavailable.
Setup tokens cannot read this inventory.

These endpoints display inventory only and never redeem a reset.

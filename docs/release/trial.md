# The finite manual trial (P3)

`gaceta trial` is a bounded, consent-gated, **manual** way to watch the real
pipeline run — the same code the daily paper uses — on synthetic data that
never leaves the machine. It exists so a person (a pilot reader, a developer,
a reviewer) can exercise `ingest → embed → cluster → write → edition → render`
by hand, with every guarantee below enforced by code and covered by
`tests/test_trial.py`.

Nothing here fetches a feed, calls a paid model, publishes anything or reads a
credential. Nothing schedules anything. Every step is a person typing a command.

## Commands

| Command | Does | Writes |
|---|---|---|
| `trial preview` | state, and exactly what `start`/`run` would do (or why not) | nothing |
| `trial start` | create the trial: synthetic reader + feed + isolated db | consent |
| `trial run` | one real pipeline pass = today's single run | consent |
| `trial status` | runs of ≤3, day of 7, lock, snapshots | nothing |
| `trial stop` | end the trial's runs — final, no resume | consent |
| `trial keep` | preserve the artifacts for the record | consent |
| `trial remove` | delete the trial and its files (stop first; `--really` if kept) | consent |

Consent: every write action prints its plan first and needs `--yes` (or a
typed `yes` when stdin is a terminal). Without it, nothing is written.

## The bounds

- **One run per local day.** At most one `run` per calendar day; a second on
  the same day refuses before consent is even asked.
- **Three runs, total, across seven calendar days.** The trial window is
  `start_day .. start_day+6`, and the trial's whole life holds at most three
  runs. The fourth refuses on any day — run 3/3 is terminal for runs even if
  four window days remain, and even after a crash (see below).
- **No catchup.** Unused runs never carry to another day and missed days
  cannot be backfilled: `run` always builds *today's* edition. Missed days
  cost nothing and grant nothing.
- **Reservations are never refunded.** A run is spent by an fsynced state
  write **before** the pipeline starts, so a run that fails, is interrupted
  (Ctrl-C) or is killed mid-flight still consumes its day and one of the
  three. That is the honest accounting for a bounded trial: the reservation
  is the cost.
- **Failed runs.** A failed run records `status: "failed"` plus the error,
  keeps its reservation (day + lifetime) and never poisons the trial: the
  next day's run proceeds normally. An interrupted (Ctrl-C) or killed run
  keeps the same accounting with `status: "reserved"`.
- **Budgets, bounded for the whole lifetime.** Each run's fake-chat calls are
  capped (`--max-llm-calls`, `--max-change-calls`; defaults 12/6, hard caps
  60/30). Because the trial is at most three runs, lifetime fake-chat calls
  are bounded **by construction**: ≤ 3 × 60 = **180** write calls and ≤ 3 × 30
  = **90** change calls, whatever happens on any run. A spent budget never
  blocks the paper — cards simply stay raw, and the run reports it.

## Durability: lock + reservations

- `run` holds `run.lock` (pid + host, created atomically with O_EXCL). A second
  run refuses while the lock is live. A killed run leaves it behind; the next
  day's run refuses until `run --recover-lock` clears it — and recovery never
  refunds the interrupted run. A crashed run 3/3 is terminal: recovery cannot
  buy a fourth. `status` shows a stale lock as such.
- Every refusal happens before any write: a refused run leaves the state
  bytes untouched, and the checks run again under the lock so two racing
  `run`s cannot both spend the same day.

## Offline-only limits (explicit)

The trial runs offline and uncredentialed. This is not a convention but a
gate, re-verified before **every** run and refused with no run spent:

- the only feed is the synthetic file `start` wrote inside the trial dir; a
  `url =` feed and `[sources.rss.transcripts]` (which use the network) are
  refused;
- every link in that feed points at the reserved `.invalid` domain — nothing
  in it is fetchable, nothing is a real source;
- `[embed]` and `[llm.editor]` are pinned to the deterministic fakes;
  `base_url`, `api_key_env`, `model` and `extra_body` are refused (no
  endpoints, no credentials, no paid inference);
- no `[schedule]`, and nothing here starts timers, cron or daemons; an
  edition that comes back with `next_update` is refused;
- no `[conditions]` plugin boxes, no `[cluster].confirm`, no reader profile.

Edit the trial reader and runs stop — by design.

## Isolation and honesty

- Everything lives under `<GACETA_DATA>/trials/<id>/`: its own `gaceta.db`,
  reader, feed, editions and renders. The main database, readers and editions
  are never touched.
- The reader is synthetic: twelve generated items in four groups, one local
  feed file.
- The synthetic feed is static, so a later day's run is an honest near-no-op
  (0 new items, 0 rewrites, 0 fake-chat calls) that still spends its run: a
  rerun demonstrates the pipeline, never invents news.
- `keep`/`stop` are terminal states; `remove` needs `stop` first (two
  consents, never one) and `--really` on top for a kept trial. Only one live
  trial may exist at a time.

## Why this shape

`docs/release/pilot.md` invites readers to try the paper on their own machine
and asks how many separate days they came back. The trial mirrors that
protocol in miniature: a person may run the pipeline once per day, on at most
three separate days inside a seven-day window, by hand, without any chance of
spending money, leaking data or accidentally publishing. When the trial is
over the artifacts are either kept for the record or deleted; nothing lingers
half-alive.

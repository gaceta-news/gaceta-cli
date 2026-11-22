# Desk recipes: sharing a setup, never a reader

`gaceta recipe` shares the **shape** of a desk — which public feeds it
follows and which section labels it uses — as a small, inert JSON file
called a **desk recipe**. Nothing else rides along: no items, no stories, no
marks, reactions or taste, no provider settings, no api key names, no local
paths, no database. It is how one gaceta reader hands another person the
skeleton of a paper they can make their own.

```
gaceta recipe export  --reader main --feed bbc-tech --feed arxiv-cs --label tech \
                      [--title T] [--langs es,en] [--link https://…/] [--consent]
gaceta recipe inspect desk.json          # anyone: read-only preview, safe parser
gaceta recipe import  desk.json --as a-friend-desk --consent
```

## Why this shape

- **Private by default.** The export is an allowlist, not a denylist:
  *nothing* is shared unless you name it on the command line
  (`--feed <id>`, `--label <id>`, repeatable). Unselected feeds and labels
  never travel, not even as counts inside the file; the preview says only
  *how many* stayed private, never their names.
- **Explicitly selected, or refused.** A `--feed` slug that does not exist,
  a feed that reads a local `file =`, a label whose id is not a url-safe
  slug — each refuses the whole export. Silence never means "shared".
- **Public web urls only.** Every carried url must be http(s) with a
  public-looking host: a dotted ascii hostname (or a global ip literal), no
  embedded credentials, no `localhost`/`.local`/lan names, no loopback or
  private addresses. The check is syntactic — recipe tools never do DNS or
  fetch anything; ingest re-runs its own SSRF guard when the feed is
  actually read.
- **Canaries never ride.** The owner's privacy terms
  (`[privacy] pinned` / `household`) are checked against every string the
  recipe would carry — feed names, label titles, keywords, the `--title`,
  the link. A selection that touches one refuses outright, and the error
  never names the term.
- **Safe parser, fail closed.** `inspect` and `import` read exactly one
  file with an allowlist parser: every level (recipe, feed, label, link)
  allows only the keys this surface itself writes and refuses everything
  else, with hard caps on file size, feed/label counts, list lengths and
  string lengths. Executable config, provider tables (`embed`, `llm`),
  `api_key_env` names, adapter paths, nostr keys, plugin tables, arbitrary
  settings — there is no key for any of them to sneak in through, and a
  recipe is data, never code: nothing is evaluated, merged or imported
  verbatim.
- **Preview before consent.** `export` and `import` print the full preview —
  exactly what would be written, and where — and write **nothing** unless
  `--consent` is passed. `inspect` is always read-only: one file, no data
  dir, no database, no reader config.
- **New, isolated, independent readers.** `import --as <id>` creates a
  brand-new reader and refuses if anything already exists at that path —
  never an overwrite, never a merge. The recipe is not bound to any reader
  (unlike the agent export), so every recipient is independent: importing
  the same recipe twice creates two readers that share nothing. The
  importer *regenerates* a fresh `reader.toml` from gaceta's own template
  using only the allowlisted values — fake model providers, empty privacy
  canaries — so recipe bytes never reach a config file as-is, and the
  recipe cannot choose providers, keys or any other setting for the
  recipient.
- **The renderer link is explicit and public-only.** `--link` is opt-in,
  must be https, and is recorded in the recipe as `privacy: "public"` —
  the parser demands that exact marking and refuses any link that is not
  explicitly public. `inspect` shows it as a public-only renderer link
  (point it at your `gaceta render --max-privacy public` output, wherever
  you host it); `import` keeps it in the generated config as a comment —
  a place to visit in a browser, never a setting gaceta reads.

## The file

```json
{
  "schema_version": 1,
  "kind": "gaceta.desk-recipe",
  "recipe": {
    "title": "Ana's tech desk",
    "langs": "es,en",
    "feeds": [
      {"id": "bbc-tech", "name": "BBC Technology",
       "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
      {"id": "tube", "name": "Some Channel",
       "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC…",
       "kind": "youtube"}
    ],
    "labels": [
      {"id": "tech", "title": "Technology", "kinds": ["rss"],
       "source_prefixes": [], "keywords": ["chip", "solar"]}
    ]
  },
  "link": {"url": "https://paper.example/ana/", "privacy": "public",
           "about": "the sender's public-only rendered paper …"},
  "privacy": {"max_level": "public",
              "carries": "a desk recipe carries configuration only: …"}
}
```

| Level | Keys | Notes |
|---|---|---|
| top | `schema_version`, `kind`, `recipe`, `link`?, `privacy` | `link` optional; `privacy` must equal the exact public-only contract block |
| `recipe` | `title`?, `langs`?, `feeds`, `labels` | `langs` optional comma list the recipient may ignore |
| feed | `id`, `name`?, `url`, `kind`? | `kind` ∈ `rss`, `youtube`; url = public web |
| label | `id`, `title`?, `kinds`?, `source_prefixes`?, `keywords`? | a `[[sections]]` block's match rules |
| `link` | `url`, `privacy`, `about`? | https only, `privacy` must be `"public"` |

Everything else — at any level — refuses with exit code 2 and no data on
stdout. The same parser validates gaceta's own exports (the builder must
satisfy its own contract) and everything a recipient hands it.

## Defaults

- `recipe export` writes `<data>/recipes/<reader>/<title-slug>.json`
  (`--out` to choose another `.json` path). Re-exporting over gaceta's own
  artifact is a refresh; overwriting a *reader* is something only
  `import` would do, and it never does.
- `recipe import --as x` writes `<data>/readers/x/reader.toml` (exclusive
  create) and prints the follow-up: `gaceta ingest rss --reader x`, then
  `embed`, `cluster`, `write`, `edition`, `render`.

The surface opens no database anywhere: export reads the reader's TOML
only; inspect/import read one recipe file; the only writes are the two
consent-gated files above.

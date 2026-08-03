<p align="center">
  <img src="assets/logo.png" alt="ScholaInvenio" width="760">
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-brightgreen?style=flat-square" alt="Python 3.12+"></a>
  <a href="https://docs.docker.com/get-docker/"><img src="https://img.shields.io/badge/Docker-required-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker required"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/version-1.0.0-blue?style=flat-square" alt="Version 1.0.0">
</p>

ScholaInvenio is a self-hosted academic paper search tool. It collects
results from OpenAlex, DBLP, and Google Scholar in every search, merges them
with a single keyword syntax, exports them to XML, and lets you refine that list through
successive local filter passes without ever losing an earlier pass. It runs
as a Docker image and serves its own web GUI, so there is nothing to install
beyond Docker itself.

## Get started

```bash
git clone <this-repo>
cd scholainvenio
cp .env.example .env
# edit .env and set OPENALEX_API_KEY, see "OpenAlex API key" below
./run.sh up
```

Open <http://localhost:8000>.

```bash
./run.sh down    # stop
./run.sh logs    # tail logs
```

`docker compose` is used automatically if the plugin is installed, otherwise
`run.sh` falls back to plain `docker run`. Either way, `.env` in the project
root is picked up.

<details>
<summary>Without run.sh</summary>

```bash
docker build -t scholainvenio:1.0.0 .
docker run -d --name scholainvenio -p 8000:8000 \
  -v "$PWD/data:/data" --shm-size=1g --env-file .env \
  scholainvenio:1.0.0
```

</details>

## OpenAlex API key

OpenAlex is free but rate-limited by a small daily budget: **100
requests per day without a key, 10,000 with one**, per the [official
pricing](https://openalex.org/pricing). A single search can use several
requests, so the unauthenticated limit runs out fast. Get a key.

1. Go to <https://openalex.org/settings/api> and generate a free key.
2. Run `cp .env.example .env`, then set `OPENALEX_API_KEY=<your key>`.

Without a key, the GUI shows a persistent banner explaining the limit. If the
daily budget runs out mid-search, whatever was already fetched is kept, and a
note explains why the results stopped there. It never fails silently with
zero results.

## Features

- **One keyword syntax, multiple providers.** Exact phrases, `OR`,
  `-exclude`, wildcards, `author:`, `intitle:`, `source:`, and a date range,
  all auto-translated per provider.
- **Multi-pass refinement.** Round 1 fetches from the network. Every round
  after that filters the *previous round's list*, with no re-fetching, and
  every round can be rolled back on its own.
- **XML export**, one file per round, all rounds in a single session
  directory.
- **A GUI with no build step**: static HTML, CSS, and vanilla JS, no
  framework, no bundler.
- **Sort** any round by year or citation count.
- **Load more** beyond the initial batch, appended straight into the same
  round's XML file.
- **Natural-language session names**, generated from the query
  (`author PJ Hayes, words "naive physics" -survey, 1990~2020`) instead of
  raw keyword syntax.

## Providers

| | Official API | Speed | Rate limit |
|---|---|---|---|
| **OpenAlex** | yes | fast | daily budget, see above |
| **DBLP** | yes | moderate (rate-limited politely) | none published |
| **Google Scholar** | no, browser-scraped | very slow | frequent blocks |

Every search queries all three concurrently and merges the results — there's
no per-search opt-in. If one fails, the others' results still come back: a
provider that succeeds with zero results is treated differently from one
whose request actually failed, so a blocked Google Scholar never sinks the
rest of a search. Which providers actually contributed to a round is shown
next to its result count, as a small toggle per source: click one to hide
(or bring back) just the papers that provider found, without re-fetching.
Hiding is a display choice, not a query — a paper two providers both found
stays visible until every one of its sources is hidden.

Google Scholar's own deliberate per-result delay (5-10s, see below) makes it
far slower than the other two; it's capped to its own small result count
per search independently of the overall ceiling (`GSCHOLAR_MAX_RESULTS`,
default 20) so that always querying it doesn't mean always waiting minutes.

DBLP is a manually curated, **computer-science-only** bibliography with no
abstracts and no citation counts, but it's often more complete than OpenAlex
for recent conference proceedings. Confirmed directly: OpenAlex has
essentially stopped ingesting USENIX Security's program since 2022 (2-6
works/year, none of them linked to any venue record, and the papers aren't
findable under any venue, linked or not — OpenAlex just doesn't seem to have
them), while DBLP has the full program up the same day the proceedings go
live. It's a second, independent net rather than a replacement: what one
misses, the other tends to have. Outside computer science, it won't have
anything.

<details>
<summary>How the DBLP path works</summary>

No API key, CC0-licensed public data. `source:` is resolved against DBLP's
own venue index first (`/search/venue/api`), which — unlike OpenAlex — is
already a clean, canonical string with no year or edition number baked in,
so a sister venue that merely shares words (`IEEE Symposium on Security and
Privacy` vs. `IEEE *European* Symposium on Security and Privacy`) is told
apart the same way OpenAlex's unlinked-edition matching is.

DBLP's own text search has no notion of an exact phrase, negation, or
wildcards, and nothing to search inside an abstract. Rather than trying to
reproduce the OpenAlex native-filter/residual split, DBLP is only trusted for
*recall*: a best-effort query (free words, `author:`, resolved `venue:`)
fetches a candidate set, and the *entire* original query is then re-applied
locally — the same proven check spec 5's refine search already uses —
before anything is shown. A year range is queried with DBLP's native
`year:` facet one year at a time (it has no range syntax), up to 6 years;
past that it falls back to fetching broadly and filtering locally.

Paginated requests are spaced out to stay a reasonable citizen of a free,
volunteer-run service, so a search spanning several unresolved venues or
several years is slower than OpenAlex.

</details>

<details>
<summary>How the Google Scholar path works</summary>

Scholar has no API and actively blocks automation, so this goes through two
layers.

1. [`scholarly`](https://pypi.org/project/scholarly/)`.search_pubs()` passes
   Scholar's own keyword syntax straight through, so nothing needs
   translating.
2. If that gets blocked, a Chrome instance launched via
   [`undetected-chromedriver`](https://pypi.org/project/undetected-chromedriver/)
   fetches the same result pages directly. Automation fingerprints
   (`navigator.webdriver`, the `HeadlessChrome` user-agent string) are
   stripped so the fetch doesn't announce itself as a bot, and the first
   page is reached by actually typing the query into Scholar's own search
   box on its homepage, character by character with human-scale pauses,
   rather than loading a constructed results URL directly — a page real
   visitors never land on without searching for something first.

To avoid an IP ban, it sleeps 5 to 10 random seconds between papers. Tune it
with:

```bash
GSCHOLAR_SLEEP_MIN=8 GSCHOLAR_SLEEP_MAX=15 ./run.sh up
```

If a fetch is blocked partway through, whatever was already collected is
kept instead of discarded.

An IP ban, if one happens anyway, is Scholar banning the requesting IP, not
the tool — routing through a proxy means a ban lands there instead of on the
host, so the rest of the network (and the tool itself, next search) keeps
working. Off by default:

```bash
GSCHOLAR_PROXY=tor ./run.sh up                    # a local Tor daemon
GSCHOLAR_PROXY=free ./run.sh up                    # scholarly's free-proxy pool
GSCHOLAR_PROXY=http://user:pass@host:port ./run.sh up   # a specific proxy
```

Skip Chromium entirely if you only need OpenAlex and DBLP; it accounts for
most of the image's size.

```bash
docker build --build-arg BUILD_BROWSER=0 -t scholainvenio:1.0.0 .
```

</details>

## Keyword syntax

Click **⚙ Keywords** next to the search box for a builder with one field per
operator. Typing into any field updates a live preview of the assembled
query, which you then apply to the main search box. The syntax can also be
typed directly.

| Meaning | Syntax | Example input | Result |
|---|---|---|---|
| Exact phrase | `" "` | `temporal reasoning` | `"temporal reasoning"` |
| Union | `OR` | `ontology, taxonomy` | `ontology OR taxonomy` |
| Exclude | `-` | `survey, review` | `-survey -review` |
| Wildcard | `*` | `reason*` | `reason*` |
| Author | `author:` | `PJ Hayes` | `author:PJ author:Hayes` |
| Title | `intitle:` | `naive physics` | `intitle:naive intitle:physics` |
| Venue | `source:` | `Artificial Intelligence` | `source:Artificial source:Intelligence` |
| Date range | none | `1990` to `2020` | kept out of the query string, see below |

- **Author, title, and venue repeat the syntax per word**, a Google Scholar
  convention. `PJ Hayes` becomes `author:PJ author:Hayes`. Group with quotes
  instead: `"PJ Hayes"` becomes `author:"PJ Hayes"`.
- **Whitespace means AND.**
- Search targets the whole record, not just the title.
- Wildcards work inside a word (`reason*` matches *reasoning*) and inside a
  phrase (`"deep * network"`).
- **Date range** has no Google Scholar syntax, so it's applied after
  fetching instead: natively through OpenAlex's `publication_year` filter,
  or as a local post-filter for Google Scholar. A paper with no readable
  year is dropped once a range is set.

<details>
<summary>Per-provider translation table</summary>

| Syntax | OpenAlex | DBLP | Google Scholar |
|---|---|---|---|
| `"exact phrase"` | native + local recheck | local recheck only | native |
| `OR` | native | local recheck only | native |
| `-exclude` | native, `NOT` | local recheck only | native |
| `*` wildcard | local recheck | local recheck only | native |
| `author:` | native, `raw_author_name.search` | native `author:` facet + local recheck | native |
| `intitle:` | native, `title.search` | local recheck only | native |
| `source:` | name resolved to an ID, then native | name resolved via `/search/venue/api`, then native `venue:` facet + local recheck | not native; stripped from the query and checked locally instead |
| date range | native, `publication_year` | native `year:` facet (one year at a time) up to a 6-year span, else local recheck | local recheck |

DBLP's "local recheck only" rows aren't a weaker version of the other
providers' handling — DBLP has no full-text index or abstract to check a
phrase or exclusion against natively in the first place, so every DBLP
result is re-verified against the *complete* original query locally before
it's shown, not just the parts a native filter might have gotten wrong.

"Local recheck" means the provider gets an approximate query, and the exact
condition is re-verified afterward, locally. A condition a provider already
handled natively is never rechecked, because doing so would re-judge
results a provider's full-text index already matched, using this tool's
thinner metadata, and silently drop good papers.

OpenAlex specifics, confirmed against its docs:

- Only a limited set of fields accept a `.search` suffix (`title.search`,
  `abstract.search`, `title_and_abstract.search`, `raw_author_name.search`,
  `fulltext.search`, and a few more). There is no text filter for venue
  names.
- `filter` syntax: `|` is OR (max 100 values), `!` is NOT, `2020-2024` is a
  range.
- Deep pagination uses a `cursor` (the `page` parameter caps at 10,000
  results).
- The docs explicitly recommend resolving ambiguous names to IDs first,
  which is why venue search works the way it does below.

### Venue (`source:`) search

Per spec, `Artificial Intelligence` splits into `source:Artificial
source:Intelligence`, but that's one venue name, not two conditions, so the
tokens are rejoined before lookup. (`author:` tokens stay separate, since
`author:Hayes author:McCarthy` can mean a paper co-authored by two
different people.)

OpenAlex has no text filter for venue names, so this happens in two steps:

```
source:USENIX source:Security source:Symposium
  1) GET /sources?search=USENIX Security Symposium  ->  source ID(s)
  2) GET /works?filter=locations.source.id:<id>|<id>|...
```

Three details matter here:

- **Filters on `locations.source.id`, not `primary_location.source.id`.**
  Many conference papers have no source under `primary_location` and only
  appear under `locations`. Measured on one venue: 625 works under
  `primary_location` versus 1222 under `locations`.
- **Abbreviations are left to OpenAlex's own index**, not hardcoded here.
  `/sources` search also matches `abbreviated_title` and `alternate_titles`.
- **An overly narrow match gets relaxed.** `/sources` search ANDs its
  tokens, so `IEEE Requirements Engineering` only matches records
  containing all three words, missing the actual journal, which doesn't say
  "IEEE" in its name. If the matched venues' combined paper count looks too
  small, the leading token is dropped and the search retried, then merged
  in. This also absorbs OpenAlex splitting one conference across several
  source records by year or naming variant.

Which venues a query resolved to is shown in the round's notes, so an
unexpected result count can be traced back to the actual match.

</details>

## What's collected

Title, full author list, publisher, venue, year, DOI, abstract, links,
citation count, and which provider or providers it came from.

OpenAlex returns the full abstract as an inverted index, which is
reconstructed into plain text. Google Scholar gives only a snippet. DBLP has
no abstract at all — it's a bibliography, not a full-text index. Enabling
**enrich abstracts from source pages** additionally fetches each result's
landing page and extracts a full abstract from its meta tags where possible,
at the cost of speed; this can help DBLP and Scholar results alike, though
it depends entirely on whether the paper's own landing page happens to carry
one.

The detail panel tags the abstract's source: *from API*, *extracted from
source*, or *Scholar snippet*.

## Interface

- **Round-by-round lists stack horizontally** and scroll, each headed by
  its natural-language name, its actual query syntax, its scope and date
  range and source, and a sort selector.
- **Lists show titles only.** Clicking one opens the full record in the
  side panel, not a popup.
- **A round's `×`** deletes that round's XML and rolls back to the
  previous round. Deleting round *n* also removes every round derived from
  it.
- **The last round's footer** is where the next round is entered, using the
  same keyword syntax plus a **title only** or **title + abstract only**
  scope checkbox. `author:`, `intitle:`, and `source:` look at their own
  field regardless of that checkbox.
- **The saved sessions panel** lets each row's `×` delete that session's
  whole directory immediately, with no confirmation.
- **Clicking the logo or title** returns to an empty session (home).

Round *n* filters the *previous round's* list. It never touches the
network.

<details>
<summary>Loading more than the ceiling</summary>

A single fetch caps at 1000 results per provider. Past that, a bubble above
the list shows the actual total and offers to fetch 1000 more.

- Fetched results are appended to that round's existing XML, not written to
  a new file.
- Providers resume from a cursor, so nothing already seen is fetched again
  (any overlap is still deduplicated by ID).
- Any round derived from this one is now based on stale data, so it's
  deleted, with a note explaining why.

</details>

<details>
<summary>XML layout</summary>

One session, meaning one branch of searching, is one directory. Each round
gets its own file.

```
data/sessions/20260729-114233_words-knowledge-representation/
├── round_01.xml     # round 1, provider fetch; "load more" appends here
├── round_02.xml     # round 2, filtered from round 1
├── round_03.xml     # round 3, filtered from round 2
└── session.xml      # round index
```

```xml
<scholarSearch version="1.0">
  <meta>
    <sessionId>...</sessionId>
    <round>2</round>
    <query>author:Hayes -survey</query>
    <scope>title_abstract</scope>      <!-- title | title_abstract | all -->
    <sourceRound>1</sourceRound>       <!-- 0 means fetched from providers -->
    <label>author Hayes, words -survey</label>
    <providers>
      <provider>openalex</provider>
      <provider>gscholar</provider>
    </providers>
    <continuation>                     <!-- resume point for "load more" -->
      <cursor provider="openalex">IlsxNjA5NDU5MjAwMDAwLC...</cursor>
      <total provider="openalex">2438</total>
    </continuation>
    <period><from>1990</from><to>2020</to></period>
    <createdAt>2026-07-29T11:42:33Z</createdAt>
    <resultCount>7</resultCount>
  </meta>
  <papers>
    <paper id="a1b2c3d4e5f6a7b8">
      <title>...</title>
      <authors count="2"><author>...</author><author>...</author></authors>
      <publisher>...</publisher>
      <venue>...</venue>
      <year>1979</year>
      <doi>10.1016/b978-1-4832-1447-4.50010-9</doi>
      <abstract source="api">...</abstract>       <!-- api | snippet | fulltext -->
      <citedBy>1234</citedBy>
      <sources>
        <source>openalex</source>
        <source>gscholar</source>
      </sources>
      <links><link rel="primary">...</link><link rel="pdf">...</link></links>
    </paper>
  </papers>
</scholarSearch>
```

The root element keeps the name `scholarSearch` from an earlier version of
this project. It's part of the on-disk format, so it's left as-is rather
than churning existing data over a rename.

`data/` is bind-mounted to the host, so the XML survives removing the
container. On restart, the server re-reads this tree and restores every
saved session.

</details>

<details>
<summary>Configuration</summary>

| Variable | Default | Meaning |
|---|---|---|
| `OPENALEX_API_KEY` | none | recommended, 100/day without it, 10,000/day with |
| `OPENALEX_MAILTO` | none | courtesy contact when no key is set |
| `HOST_PORT` | `8000` | host-side port |
| `GSCHOLAR_SLEEP_MIN` / `MAX` | `5.0` / `10.0` | random wait in seconds per Scholar result |
| `GSCHOLAR_MAX_RESULTS` | `20` | Scholar's own result cap per search (see Providers) |
| `GSCHOLAR_PROXY` | none | `tor`, `free`, or `http://host:port` — see Providers |
| `GSCHOLAR_TOR_PORT` / `_CONTROL_PORT` / `_PASSWORD` | `9050` / `9051` / none | only used when `GSCHOLAR_PROXY=tor` |
| `CHROME_BINARY` | `/usr/bin/chromium` | Chrome or Chromium binary path |
| `CHROMEDRIVER_PATH` | `/home/scholar/chromedriver` | writable chromedriver copy, see Dockerfile |
| `DATA_DIR` | `/data` | where XML is stored |
| `TZ` | `UTC` | container timezone |

</details>

<details>
<summary>Multi-arch builds</summary>

The image has no OS-specific binaries, so an image built on macOS runs
unmodified on Linux.

```bash
# build for the current platform
docker build -t scholainvenio:1.0.0 .

# cross-build for both, and push to a registry
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <registry>/scholainvenio:1.0.0 --push .

# transfer a locally built image without a registry
docker save scholainvenio:1.0.0 | gzip > scholainvenio.tgz
#   on the target host:  gunzip -c scholainvenio.tgz | docker load
```

</details>

<details>
<summary>Project layout</summary>

```
app/query.py       keyword syntax builder and parser, the only place syntax rules live
app/translate.py   syntax to each provider's native query, plus residual conditions
app/openalex.py    OpenAlex fetch: ID resolution, cursor pagination, budget handling
app/dblp.py        DBLP fetch: venue resolution, recall query, full local re-verification
app/venue_match.py shared "is this the same venue" check (openalex.py and dblp.py)
app/gscholar.py    Google Scholar fetch: scholarly plus undetected-chromedriver
app/scholar.py     Scholar result HTML parser and abstract enrichment
app/providers.py   concurrent provider execution, failure versus zero-result handling
app/merge.py       cross-provider dedup and field merging
app/filters.py     local search-operator evaluation and date-range filter
app/naming.py      query to natural-language session name
app/xmlstore.py    XML serialization, restore, and rollback
app/models.py      Paper, Round, Session
app/main.py        FastAPI routes
static/            the GUI, dependency-free vanilla JS
```

</details>

## Reporting issues

Found a bug or have a feature request? Open an issue on this repository.

## License

Apache License 2.0. See [LICENSE](LICENSE).

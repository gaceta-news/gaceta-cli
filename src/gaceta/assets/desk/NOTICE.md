# NOTICE — provenance and attribution for the desk packet

## What this is

- Synthetic sample data for gaceta, generated deterministically by
  `make_desk_packet.py`: byte-identical on every rebuild — no clock, no
  randomness, no network.
- Every outlet, person, company, regulator, currency, place and event in the
  packet is invented; any resemblance to real entities is coincidental
  (full list below).
- The packet is fully offline: every link on `index.html` resolves to a file
  in this directory. The `*.example` hosts in the data are reserved invented
  names and are never contacted.
- Documents only: the sample ships with no labels, notes or scoring of any
  kind, so nothing needs to be stripped before handing it to a person, a
  pipeline or a model. (Development-side reference material is kept outside
  this directory, in the source repository, and is not part of the sample.)

## Fictional entities

- República de Valdora; capital Vado Prieto; invented currency valdoro (V$)
- Autoridad Reguladora de Aguas de Valdora (ARAV)
- Autoridad Reguladora de Energía de Valdora (AREV)
- Compañía Minera Cerro Alto, S.A.
- Generadora Marbrisa, S.A.
- Acuífero de la Serranía del Toldo (sector Vega Norte); polígono portuario
  de Puerto Gris
- Gaceta Oficial de Valdora; Agencia de Noticias Unidas de Valdora (ANUV);
  El Heraldo de Valdora; El Pulso Económico; Valdora Morning Post;
  The Enforcement Ledger
- Helena Marsal; Ferrer & Linares
- Invented hosts: gazetavaldora.example, anuv.example, elheraldo.example,
  elpulso.example, valdoramorningpost.example, enforcementledger.example

## Attribution (CC BY 4.0)

The data files in this directory — `packet.json`, `feed.rss`, `sources/` and
`index.html` — are licensed under Creative Commons Attribution 4.0
International (full text: `CC-BY-4.0.txt`). When you redistribute or adapt
them, keep this NOTICE and the license file with the data, and credit:

> Gaceta desk packet (synthetic EN/ES three-day sample), CC BY 4.0 —
> gaceta-news/gaceta-cli

The generator script (`make_desk_packet.py`) is code: it follows the
repository's AGPL-3.0 (see the repository `LICENSE`).

## Redistribution

Ship the blind unit exactly as: `packet.json`, `feed.rss`, `sources/`,
`index.html`, `NOTICE.md`, `CC-BY-4.0.txt`. Do not add
`make_desk_packet.py` to that shipment: the generator is repo-side tooling
and embeds the development-side reference material, which lives outside
this directory in the source repository and never travels with the
sample. Anyone with the source repository can replay the build byte for
byte with `python src/gaceta/assets/desk/make_desk_packet.py`.

## No live models

No live model was run to build this packet, and the packet asserts nothing
about any live model, vendor or API. It contains no scores and no runs.

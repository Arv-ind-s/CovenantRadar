# Third-party notices

Covenant Radar is distributed under the MIT licence (see [`LICENSE`](LICENSE)).
It includes, links to, or installs alongside the third-party components listed
below, each of which remains under its own licence and its own copyright.

This file discharges the attribution and notice obligations of those licences.
The reasoning behind each classification — and the obligations that need more
than a notice — is recorded in
[`docs/oss-ip-compliance.md`](docs/oss-ip-compliance.md).

Versions are those pinned in [`requirements.lock`](requirements.lock). Only
components that ship with the product are listed; build- and test-only
dependencies appear in the compliance register instead.

---

## 1. Components requiring more than attribution

### `psycopg` and `psycopg-binary` — LGPL-3.0-only

Copyright © The Psycopg Team.

This product uses psycopg, the PostgreSQL adapter for Python, licensed under
the GNU Lesser General Public License version 3. Psycopg is installed as a
separate, **unmodified** Python distribution and is loaded dynamically; no
part of it is vendored into, statically linked with, or modified by Covenant
Radar. Recipients may replace it with a modified version by installing a
different build of the package.

The complete source is available from <https://github.com/psycopg/psycopg>
and from PyPI at <https://pypi.org/project/psycopg/>. A copy of the LGPL-3.0
text is distributed with the package. On request, the source corresponding to
the exact version shipped will be provided.

The `psycopg[binary]` wheel additionally bundles **libpq** (PostgreSQL
Licence, © The PostgreSQL Global Development Group) and **OpenSSL**
(Apache-2.0, © The OpenSSL Project Authors). Their notices are included in the
wheel.

### `img2pdf` — LGPL-3.0-only

Copyright © Johannes Schauer Marin Rodrigues. Installed unmodified as a
separate distribution and dynamically loaded; the same terms and source offer
as above apply. Source: <https://gitlab.mister-muffin.de/josch/img2pdf>.

### `pi-heif` — BSD-3-Clause; wheel links libheif (LGPL-3.0)

Copyright © Alexander Piskun and contributors. The binary wheel links
**libheif** (LGPL-3.0, © Dirk Farin), unmodified and dynamically. Source:
<https://github.com/bigcat88/pillow_heif> and <https://github.com/strukturag/libheif>.

### `pyphen` — multi-licensed, LGPL-2.1-or-later elected

Copyright © Kozea and contributors. Offered under GPL-2.0-or-later,
LGPL-2.1-or-later or MPL-1.1. **Covenant Radar elects LGPL-2.1-or-later.**
Installed unmodified; source at <https://github.com/Kozea/Pyphen>.

### MPL-2.0 components — `certifi`, `ocrmypdf`, `pikepdf`

Mozilla Public License 2.0 applies file-by-file to the covered files of each
distribution. All three are used unmodified; their source is available from
the projects' repositories and from PyPI. Should a covered file ever be
modified, that modification stays under MPL-2.0.

---

## 2. Vendored assets

### HTMX 2.0.4 — MIT

Copyright © 2020 Big Sky Software. Licence text: `src/covenant_radar/web/static/vendor/htmx/LICENSE`.

### Fonts — SIL Open Font License 1.1

Redistributed unmodified and bundled with software:

- **Source Serif 4** — © Adobe Inc. (`radar-serif.ttf`)
- **IBM Plex Mono** — © IBM Corp. (`radar-mono-regular.ttf`, `radar-mono-semibold.ttf`, `radar-mono-bold.ttf`)
- **IBM Plex Sans** — © IBM Corp. (`radar-sans.ttf`)
- **Noto Sans Devanagari** — © Google LLC (`radar-devanagari.ttf`)

Full licence text, upstream sources and SHA-256 digests:
`src/covenant_radar/web/static/fonts/LICENSES.md`.

---

## 3. System components loaded at runtime

Supplied by the deployment platform, not redistributed by this repository. The
image that installs them carries its own notices.

- **Pango**, **GLib** — LGPL-2.1-or-later
- **cairo** — LGPL-2.1-or-later or MPL-1.1
- **HarfBuzz**, **fontconfig** — MIT / MIT-style
- **Tesseract OCR** — Apache-2.0, invoked as a separate process
- **PostgreSQL** — PostgreSQL Licence

---

## 4. Attribution by licence

Each component below is used unmodified. Its licence requires retention of its
copyright and licence text, which is distributed inside each installed
package. Apache-2.0 components' `NOTICE` files, where supplied, are retained
in the same way.

### Apache-2.0

`deprecation` 2.1.0, `importlib-metadata` 8.5.0, `opentelemetry-api` 1.29.0, `opentelemetry-sdk` 1.29.0, `opentelemetry-semantic-conventions` 0.50b0, `prometheus-client` 0.21.1, `pyopenssl` 22.0.0, `pysaml2` 7.5.4, `python-multipart` 0.0.20, `requests` 2.34.2, `tenacity` 9.1.4, `tzdata` 2026.3, `zopfli` 0.4.3

### Apache-2.0 OR BSD-2-Clause

`packaging` 26.3

### Apache-2.0 OR BSD-3-Clause

`cryptography` 44.0.3

### BSD-2-Clause

`pygments` 2.21.0, `wrapt` 2.4.0

### BSD-3-Clause

`authlib` 1.4.1, `click` 8.5.0, `cloudpickle` 3.1.2, `cssselect2` 0.9.0, `httpcore` 1.0.9, `httpx` 0.28.1, `idna` 3.19, `itsdangerous` 2.2.0, `jinja2` 3.1.6, `joblib` 1.6.0, `lxml` 6.1.2, `markupsafe` 3.0.3, `numpy` 2.2.6, `pandas` 3.0.5, `pycparser` 3.0, `pydyf` 0.12.1, `pypdf` 5.9.0, `python-dateutil` 2.9.0.post0, `python-dotenv` 1.2.3, `scikit-learn` 1.6.1, `scipy` 1.18.1, `starlette` 1.6.0, `threadpoolctl` 3.6.0, `tinycss2` 1.5.1, `uvicorn` 0.52.4, `weasyprint` 63.1, `webencodings` 0.6.1, `websockets` 17.1

### BSD-3-Clause OR Apache-2.0; wheel bundles PDFium (BSD-3-Clause)

`pypdfium2` 5.13.0

### BSD-3-Clause; wheel links libheif (LGPL-3.0)

`pi-heif` 1.4.0

### GPL-2.0-or-later OR LGPL-2.1-or-later OR MPL-1.1

`pyphen` 0.18.1

### LGPL-3.0-only

`img2pdf` 0.6.3, `psycopg` 3.2.13, `psycopg-binary` 3.2.13

### MIT

`alembic` 1.14.1, `annotated-doc` 0.0.5, `annotated-types` 0.8.0, `anyio` 4.14.2, `apscheduler` 3.11.3, `argon2-cffi` 23.1.0, `argon2-cffi-bindings` 26.1.0, `brotli` 1.2.0, `charset-normalizer` 3.5.1, `deprecated` 1.3.1, `elementpath` 4.8.0, `et-xmlfile` 2.0.0, `fastapi` 0.141.1, `fonttools` 4.63.0, `h11` 0.16.0, `httptools` 0.8.0, `linkify-it-py` 2.2.0, `mako` 1.4.1, `markdown-it-py` 4.2.0, `mdurl` 0.1.2, `openpyxl` 3.1.5, `pdfminer-six` 20260107, `pdfplumber` 0.11.10, `pluggy` 1.6.0, `pydantic` 2.11.10, `pydantic-core` 2.33.2, `pydantic-settings` 2.7.1, `python-docx` 1.1.2, `pyyaml` 6.0.3, `rich` 15.0.0, `six` 1.17.0, `sqlalchemy` 2.0.52, `tinyhtml5` 2.1.0, `typing-inspection` 0.4.4, `tzlocal` 5.4.4, `urllib3` 2.7.0, `watchfiles` 1.2.0, `xmlschema` 2.5.1, `zipp` 4.1.0

### MIT AND PSF-2.0

`greenlet` 3.5.5

### MIT OR Apache-2.0

`structlog` 25.5.0

### MIT-0

`cffi` 2.1.1

### MIT-CMU

`pillow` 12.3.0

### MPL-2.0

`certifi` 2026.7.22, `ocrmypdf` 16.13.0, `pikepdf` 10.12.0

### PSF-2.0

`defusedxml` 0.7.1, `typing-extensions` 4.16.0

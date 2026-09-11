# OSS and IP compliance register

This record answers one question for every third-party component Covenant
Radar contains: **what licence governs it, what obligation that licence
creates, and where in this repository that obligation is discharged.**

It covers the four places a third-party component can enter the product:

| Entry point | Governed by | Register section |
|---|---|---|
| Python packages resolved into the distributable | `pyproject.toml` → `requirements.lock` | [§4](#4-distributed-runtime-components), [§5](#5-transitive-runtime-components) |
| Python packages used only to build and test | `pyproject.toml` `[dev]` extra | [§6](#6-build-and-test-only-components) |
| Assets copied into the tree | `src/covenant_radar/web/static/vendor`, `…/static/fonts` | [§7](#7-vendored-assets) |
| Programs and images invoked but never redistributed | system packages, CI services, GitHub Actions | [§8](#8-system-and-out-of-tree-components) |

Data and model IP — training corpora, prompts, model weights — is covered in
[§9](#9-data-and-model-ip).

**Outbound licence.** Covenant Radar itself is distributed under the MIT
licence (see [`LICENSE`](../LICENSE)). Every obligation below is assessed
against that outbound licence and against distribution as an installable
Python wheel plus its resolved dependency set.

**Currency.** The register describes `requirements.lock` as resolved for
Python 3.12.8. Licence facts were resolved from each distribution's own
metadata and, where that metadata was not an SPDX expression, verified against
the upstream `LICENSE` file. The `Source` column in each table records which:
`expr` = the distribution's SPDX licence expression, `cls` = its Trove
classifier, `meta` = its free-text `License` field, `file` = read from the
upstream licence file.

---

## 1. Summary

| | Runtime (distributed) | Build/test only |
|---|---|---|
| Components | 99 | 71 |
| Permissive (MIT, BSD, Apache-2.0, ISC, PSF, HPND) | 91 | 66 |
| File-level copyleft (MPL-2.0) | 3 | 3 |
| Weak copyleft (LGPL) | 5 | 1 |
| Strong copyleft (GPL) | 0 | 1 |

No GPL-licensed code is distributed with the product. Five weak-copyleft
components ship, each as an unmodified, separately installed wheel — the
configuration LGPL is written for. One GPL component sits in the build
environment only, and it is reachable only through an extra that upstream
itself offers a non-GPL alternative for; see [§10](#10-findings-and-actions).

Six declared runtime dependencies are never imported by any module in `src/`
or `evaluation/` and appear to be removable. One of them, `ocrmypdf`, is the
sole reason three copyleft wheels and the product's only AGPL system
requirement are in the graph at all. Also [§10](#10-findings-and-actions).

---

## 2. How a component enters the product

A component reaches a customer only by passing all five gates.

1. **Declaration.** It is named in `pyproject.toml` — under
   `[project].dependencies` if it ships, under the `dev` extra if it does
   not. Nothing is installed ad hoc; there is no second manifest.
2. **Pinning.** `uv pip compile` resolves the complete graph into
   `requirements.lock`, which pins an exact version for every direct and
   transitive package. CI, the security workflow and the release workflow all
   install from that file, so the set reviewed here is the set shipped.
3. **Licence review.** The component's licence is resolved and classified
   against the policy in [§3](#3-licence-policy). A new licence class, or a
   new licence for an existing component, is a reviewable change to this
   document.
4. **Vulnerability review.** `.github/workflows/security.yml` runs
   `pip-audit --requirement requirements.lock --strict` on every push, every
   pull request and weekly. A known advisory fails the build.
5. **Evidence.** `.github/workflows/release.yml` builds the wheel and sdist,
   emits a reproducible CycloneDX SBOM (`dist/bom.json`) and attaches both to
   the release as an evidence pack. The SBOM is the machine-readable twin of
   this register.

Attribution obligations are discharged by
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md), which ships in the
repository alongside `LICENSE`.

---

## 3. Licence policy

| Class | Licences | Decision | What it requires of us |
|---|---|---|---|
| **Permissive** | MIT, MIT-0, MIT-CMU/HPND, BSD-2/3-Clause, Apache-2.0, ISC, PSF-2.0 | Allowed | Retain the copyright and licence text; for Apache-2.0, retain `NOTICE` content where the distribution supplies one. No effect on our own licensing. |
| **File-level copyleft** | MPL-2.0, MPL-1.1 | Allowed unmodified | Keep the notices; make the source of *those files* available. Copyleft reaches only files of the covered work — it does not reach our code that merely calls it. Modifying such a file would put the modification under the same licence. |
| **Weak copyleft** | LGPL-2.1-or-later, LGPL-3.0 | Allowed as a separate, dynamically loaded library | Keep the notices; state the component is used and under which licence; make its source available or give a written offer; do not prevent the recipient from substituting a modified version. Satisfied by shipping it as its own installed wheel, unmodified — never by vendoring or static linking. |
| **Strong copyleft** | GPL-2.0/3.0, AGPL-3.0 | Not allowed in the distributable | Would require distributing the whole combined work under the same licence. Permitted only for programs invoked as separate processes and never redistributed ([§8](#8-system-and-out-of-tree-components)), or in a build environment that never reaches a customer. |
| **Multi-licensed** | `A OR B` | Allowed if any option is allowed | Record the elected option in this register so the election is a decision, not an accident. |

Elections on record:

- **`pyphen`** — offered as GPL-2.0-or-later **or** LGPL-2.1-or-later **or**
  MPL-1.1. **LGPL-2.1-or-later is elected.** The GPL option is not taken.
- **`cryptography`** — Apache-2.0 **or** BSD-3-Clause. **Apache-2.0 elected.**
- **`structlog`** — MIT **or** Apache-2.0. **MIT elected.**
- **`packaging`** — Apache-2.0 **or** BSD-2-Clause. **Apache-2.0 elected.**
- **`pypdfium2`** — BSD-3-Clause **or** Apache-2.0. **BSD-3-Clause elected**;
  the bundled PDFium binary is itself BSD-3-Clause.

---

## 4. Distributed runtime components

Direct dependencies, as declared in `pyproject.toml` `[project].dependencies`.
These ship with the product and their obligations are live.

| Component | Version | License (SPDX) | Obligation class | Source | Role / entry point |
|---|---|---|---|---|---|
| `alembic` | 1.14.1 | MIT | Permissive | cls | Database schema migrations (`db/`, `alembic.ini`) |
| `apscheduler` | 3.11.3 | MIT | Permissive | cls | In-process job scheduler (`scheduler/`) |
| `argon2-cffi` | 23.1.0 | MIT | Permissive | cls | Argon2id password hashing (`security/passwords.py`) |
| `authlib` | 1.4.1 | BSD-3-Clause | Permissive | file | **Declared, never imported** — the OIDC client is hand-written on `httpx` (`security/oidc.py`) |
| `cryptography` | 44.0.3 | Apache-2.0 OR BSD-3-Clause | Permissive | file | Field encryption, key rotation, SAML signature verification (`security/crypto.py`, `security/saml.py`) |
| `fastapi` | 0.141.1 | MIT | Permissive | expr | HTTP application framework (`api/`, `web/`) |
| `httpx` | 0.28.1 | BSD-3-Clause | Permissive | file | Outbound HTTP: model providers, OIDC discovery (`ai/providers/`, `security/oidc.py`) |
| `itsdangerous` | 2.2.0 | BSD-3-Clause | Permissive | file | Signed session cookie (`security/sessions.py`) |
| `jinja2` | 3.1.6 | BSD-3-Clause | Permissive | file | Server-rendered templates (`web/templates/`) |
| `numpy` | 2.2.6 | BSD-3-Clause | Permissive | file | Feature vectors for the Stage-4 forecast (`ml/forecast.py`) |
| `ocrmypdf` | 16.13.0 | MPL-2.0 | File copyleft | expr | **Declared, never imported** — OCR runs a Tesseract subprocess (`documents/ocr.py`) |
| `openpyxl` | 3.1.5 | MIT | Permissive | cls | XLSX statement reader (`ingestion/`) |
| `opentelemetry-sdk` | 1.29.0 | Apache-2.0 | Permissive | cls | Tracing exporter (`observability/`) |
| `pandas` | 3.0.5 | BSD-3-Clause | Permissive | file | **Declared, never imported** |
| `pdfplumber` | 0.11.10 | MIT | Permissive | cls | PDF text and word-coordinate extraction (`documents/`) |
| `prometheus-client` | 0.21.1 | Apache-2.0 | Permissive | cls | Metrics endpoint (`observability/`) |
| `psycopg` | 3.2.13 | LGPL-3.0-only | Weak copyleft | meta | PostgreSQL DBAPI driver — loaded by SQLAlchemy from the connection URL, not imported directly |
| `pydantic` | 2.11.10 | MIT | Permissive | expr | Settings and domain validation (`config/settings.py`, `domain/`) |
| `pydantic-settings` | 2.7.1 | MIT | Permissive | cls | **Declared, never imported** — settings are hand-built on plain `pydantic` |
| `pypdf` | 5.9.0 | BSD-3-Clause | Permissive | expr | PDF page assembly for evidence bundles (`audit/bundle.py`) |
| `pysaml2` | 7.5.4 | Apache-2.0 | Permissive | file | **Declared, never imported** — the SAML SP is hand-written on `lxml` + `cryptography` (`security/saml.py`) |
| `python-docx` | 1.1.2 | MIT | Permissive | cls | DOCX memo export (`reporting/`) |
| `python-multipart` | 0.0.20 | Apache-2.0 | Permissive | cls | Multipart upload parsing — required by Starlette's `request.form()`, not imported directly |
| `scikit-learn` | 1.6.1 | BSD-3-Clause | Permissive | file | Stage-4 ML challenger models (`ml/forecast.py`) |
| `sqlalchemy` | 2.0.52 | MIT | Permissive | meta | ORM and persistence layer (`db/`, 141 modules) |
| `structlog` | 25.5.0 | MIT OR Apache-2.0 | Permissive | expr | Structured audit and application logging (`observability/`) |
| `tenacity` | 9.1.4 | Apache-2.0 | Permissive | cls | **Declared, never imported** |
| `uvicorn` | 0.52.4 | BSD-3-Clause | Permissive | expr | ASGI server (`asgi.py`, `cli.py`) |
| `weasyprint` | 63.1 | BSD-3-Clause | Permissive | file | HTML-to-PDF rendering for memos and audit bundles (`documents/render.py`, `audit/bundle.py`) |

### Obligations that need more than a notice

**`psycopg` / `psycopg-binary` (LGPL-3.0-only)** — the PostgreSQL driver, and
the only weak-copyleft component the product genuinely needs. It is installed
as its own unmodified wheel and loaded by SQLAlchemy from the connection URL;
no module in `src/` imports it and no part of it is vendored or statically
linked. That is the arrangement LGPL §4 contemplates, so the obligation is
limited to: carry the licence text, say it is used, and offer its source. All
three are discharged in `THIRD_PARTY_NOTICES.md`. The `binary` extra bundles
`libpq` (PostgreSQL Licence) and OpenSSL (Apache-2.0); both are noted there
too. **Never vendor, patch or statically link this package** — that would
convert a notice obligation into a source-disclosure obligation on the
combined work.

**`weasyprint` (BSD-3-Clause)** — permissive itself, but at runtime it binds
system Pango, cairo, HarfBuzz, fontconfig and GLib through `cffi`. Those are
LGPL/MPL libraries supplied by the deployment platform, not by us; see
[§8](#8-system-and-out-of-tree-components). It also pulls **`pyphen`**, whose
licence election is recorded in [§3](#3-licence-policy).

**`ocrmypdf` (MPL-2.0)** — declared but **never imported**. It is the sole
reason `img2pdf` (LGPL-3.0), `pi-heif` (whose wheel links libheif, LGPL-3.0)
and `pikepdf` (MPL-2.0) are in the runtime graph, and the sole reason a
Ghostscript (**AGPL-3.0**) system requirement exists at all. The product's
actual OCR path is a Tesseract subprocess in `documents/ocr.py`. See
[§10](#10-findings-and-actions).

**`certifi` (MPL-2.0)** — the CA bundle. Unmodified; file-level copyleft does
not reach calling code. Notice only.

---

## 5. Transitive runtime components

Resolved by `uv pip compile`, shipped with the product, obligations live.

| Component | Version | License (SPDX) | Obligation class | Source | Pulled in by |
|---|---|---|---|---|---|
| `annotated-doc` | 0.0.5 | MIT | Permissive | expr | via `fastapi` |
| `annotated-types` | 0.8.0 | MIT | Permissive | expr | via `pydantic` |
| `anyio` | 4.14.2 | MIT | Permissive | expr | via `httpx`, `starlette`, `watchfiles` |
| `argon2-cffi-bindings` | 26.1.0 | MIT | Permissive | expr | via `argon2-cffi` |
| `brotli` | 1.2.0 | MIT | Permissive | meta | via `fonttools` |
| `certifi` | 2026.7.22 | MPL-2.0 | File copyleft | cls | via `httpcore`, `httpx`, `requests` |
| `cffi` | 2.1.1 | MIT-0 | Permissive | expr | via `argon2-cffi-bindings`, `cryptography`, `weasyprint` |
| `charset-normalizer` | 3.5.1 | MIT | Permissive | meta | via `pdfminer-six`, `requests` |
| `click` | 8.5.0 | BSD-3-Clause | Permissive | expr | via `import-linter`, `mutmut`, `uvicorn` |
| `cloudpickle` | 3.1.2 | BSD-3-Clause | Permissive | file | via `joblib` |
| `cssselect2` | 0.9.0 | BSD-3-Clause | Permissive | file | via `weasyprint` |
| `defusedxml` | 0.7.1 | PSF-2.0 | Permissive | cls | via `py-serializable`, `pysaml2` |
| `deprecated` | 1.3.1 | MIT | Permissive | cls | via `opentelemetry-api`, `opentelemetry-semantic-conventions` |
| `deprecation` | 2.1.0 | Apache-2.0 | Permissive | cls | via `ocrmypdf` |
| `elementpath` | 4.8.0 | MIT | Permissive | cls | via `xmlschema` |
| `et-xmlfile` | 2.0.0 | MIT | Permissive | cls | via `openpyxl` |
| `fonttools` | 4.63.0 | MIT | Permissive | meta | via `weasyprint` |
| `greenlet` | 3.5.5 | MIT AND PSF-2.0 | Permissive | expr | via `playwright`, `sqlalchemy` |
| `h11` | 0.16.0 | MIT | Permissive | cls | via `httpcore`, `uvicorn` |
| `httpcore` | 1.0.9 | BSD-3-Clause | Permissive | expr | via `httpx` |
| `httptools` | 0.8.0 | MIT | Permissive | expr | via `uvicorn` |
| `idna` | 3.19 | BSD-3-Clause | Permissive | expr | via `anyio`, `httpx`, `jsonschema`… |
| `img2pdf` | 0.6.3 | LGPL-3.0-only | Weak copyleft | cls | via `ocrmypdf` |
| `importlib-metadata` | 8.5.0 | Apache-2.0 | Permissive | cls | via `opentelemetry-api` |
| `joblib` | 1.6.0 | BSD-3-Clause | Permissive | expr | via `scikit-learn` |
| `linkify-it-py` | 2.2.0 | MIT | Permissive | cls | via `markdown-it-py` |
| `lxml` | 6.1.2 | BSD-3-Clause | Permissive | meta | via `cyclonedx-python-lib`, `pikepdf`, `python-docx` |
| `mako` | 1.4.1 | MIT | Permissive | expr | via `alembic` |
| `markdown-it-py` | 4.2.0 | MIT | Permissive | cls | via `mdit-py-plugins`, `rich`, `textual` |
| `markupsafe` | 3.0.3 | BSD-3-Clause | Permissive | expr | via `jinja2`, `mako` |
| `mdurl` | 0.1.2 | MIT | Permissive | cls | via `markdown-it-py` |
| `opentelemetry-api` | 1.29.0 | Apache-2.0 | Permissive | cls | via `opentelemetry-sdk`, `opentelemetry-semantic-conventions` |
| `opentelemetry-semantic-conventions` | 0.50b0 | Apache-2.0 | Permissive | cls | via `opentelemetry-sdk` |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | Permissive | expr | via `cyclonedx-bom`, `dependency-groups`, `deprecation`… |
| `pdfminer-six` | 20260107 | MIT | Permissive | expr | via `ocrmypdf`, `pdfplumber` |
| `pi-heif` | 1.4.0 | BSD-3-Clause; wheel links libheif (LGPL-3.0) | Weak copyleft | file | via `ocrmypdf` |
| `pikepdf` | 10.12.0 | MPL-2.0 | File copyleft | expr | via `img2pdf`, `ocrmypdf` |
| `pillow` | 12.3.0 | MIT-CMU | Permissive | expr | via `img2pdf`, `ocrmypdf`, `pdfplumber`… |
| `pluggy` | 1.6.0 | MIT | Permissive | cls | via `hatchling`, `ocrmypdf`, `pytest`… |
| `psycopg-binary` | 3.2.13 | LGPL-3.0-only | Weak copyleft | meta | via `psycopg` |
| `pycparser` | 3.0 | BSD-3-Clause | Permissive | expr | via `cffi` |
| `pydantic-core` | 2.33.2 | MIT | Permissive | cls | via `pydantic` |
| `pydyf` | 0.12.1 | BSD-3-Clause | Permissive | file | via `weasyprint` |
| `pygments` | 2.21.0 | BSD-2-Clause | Permissive | expr | via `pytest`, `rich`, `textual` |
| `pyopenssl` | 22.0.0 | Apache-2.0 | Permissive | cls | via `pysaml2` |
| `pypdfium2` | 5.13.0 | BSD-3-Clause OR Apache-2.0; wheel bundles PDFium (BSD-3-Clause) | Permissive | file | via `pdfplumber` |
| `pyphen` | 0.18.1 | GPL-2.0-or-later OR LGPL-2.1-or-later OR MPL-1.1 | Weak copyleft (LGPL-2.1-or-later elected) | file | via `weasyprint` |
| `python-dateutil` | 2.9.0.post0 | BSD-3-Clause | Permissive | file | via `arrow`, `pandas`, `pysaml2` |
| `python-dotenv` | 1.2.3 | BSD-3-Clause | Permissive | meta | via `pydantic-settings`, `uvicorn` |
| `pyyaml` | 6.0.3 | MIT | Permissive | cls | via `bandit`, `detect-secrets`, `libcst`… |
| `requests` | 2.34.2 | Apache-2.0 | Permissive | cls | via `cachecontrol`, `detect-secrets`, `pip-audit`… |
| `rich` | 15.0.0 | MIT | Permissive | cls | via `bandit`, `import-linter`, `ocrmypdf`… |
| `scipy` | 1.18.1 | BSD-3-Clause | Permissive | file | via `scikit-learn` |
| `six` | 1.17.0 | MIT | Permissive | cls | via `python-dateutil`, `rfc3339-validator` |
| `starlette` | 1.6.0 | BSD-3-Clause | Permissive | expr | via `fastapi` |
| `threadpoolctl` | 3.6.0 | BSD-3-Clause | Permissive | file | via `scikit-learn` |
| `tinycss2` | 1.5.1 | BSD-3-Clause | Permissive | file | via `cssselect2`, `weasyprint` |
| `tinyhtml5` | 2.1.0 | MIT | Permissive | cls | via `weasyprint` |
| `typing-extensions` | 4.16.0 | PSF-2.0 | Permissive | expr | via `alembic`, `anyio`, `cyclonedx-python-lib`… |
| `typing-inspection` | 0.4.4 | MIT | Permissive | expr | via `fastapi`, `pydantic` |
| `tzdata` | 2026.3 | Apache-2.0 | Permissive | meta | via `arrow`, `pandas`, `psycopg`… |
| `tzlocal` | 5.4.4 | MIT | Permissive | expr | via `apscheduler` |
| `urllib3` | 2.7.0 | MIT | Permissive | expr | via `requests` |
| `watchfiles` | 1.2.0 | MIT | Permissive | cls | via `uvicorn` |
| `webencodings` | 0.6.1 | BSD-3-Clause | Permissive | file | via `cssselect2`, `tinycss2`, `tinyhtml5` |
| `websockets` | 17.1 | BSD-3-Clause | Permissive | expr | via `uvicorn` |
| `wrapt` | 2.4.0 | BSD-2-Clause | Permissive | expr | via `deprecated` |
| `xmlschema` | 2.5.1 | MIT | Permissive | cls | via `pysaml2` |
| `zipp` | 4.1.0 | MIT | Permissive | expr | via `importlib-metadata` |
| `zopfli` | 0.4.3 | Apache-2.0 | Permissive | cls | via `fonttools` |

---

## 6. Build- and test-only components

These never reach a customer. They are listed because they appear in
`requirements.lock`, and therefore in any SBOM generated from the build
environment, and because a build-chain licence still has to be a decision.

| Component | Version | License (SPDX) | Obligation class | Source | Pulled in by |
|---|---|---|---|---|---|
| `argcomplete` | 3.7.2 | Apache-2.0 | Permissive | cls | via `nox` |
| `arrow` | 1.4.0 | Apache-2.0 | Permissive | cls | via `isoduration` |
| `attrs` | 26.1.0 | MIT | Permissive | expr | via `jsonschema`, `nox`, `referencing` |
| `bandit` | 1.8.6 | Apache-2.0 | Permissive | meta | direct |
| `boolean-py` | 5.0 | BSD-2-Clause | Permissive | meta | via `license-expression` |
| `cachecontrol` | 0.14.4 | Apache-2.0 | Permissive | expr | via `pip-audit` |
| `cfgv` | 3.5.0 | MIT | Permissive | meta | via `pre-commit` |
| `chardet` | 5.2.0 | LGPL-2.1-or-later | Weak copyleft | cls | via `cyclonedx-bom` |
| `colorama` | 0.4.6 | BSD-3-Clause | Permissive | file | via `bandit`, `colorlog`, `pytest` |
| `colorlog` | 6.12.0 | MIT | Permissive | cls | via `nox` |
| `coverage` | 7.16.0 | Apache-2.0 | Permissive | meta | via `mutmut`, `pytest-cov` |
| `cyclonedx-bom` | 7.3.1 | Apache-2.0 | Permissive | cls | via `cyclonedx-py` |
| `cyclonedx-py` | 1.0.1 | Apache-2.0 | Permissive | file | direct |
| `cyclonedx-python-lib` | 10.4.1 | Apache-2.0 | Permissive | cls | via `cyclonedx-bom`, `pip-audit` |
| `dependency-groups` | 1.3.2 | MIT | Permissive | expr | via `nox` |
| `detect-secrets` | 1.5.0 | Apache-2.0 | Permissive | cls | direct |
| `distlib` | 0.4.3 | PSF-2.0 | Permissive | cls | via `virtualenv` |
| `filelock` | 3.32.4 | MIT | Permissive | expr | via `cachecontrol`, `python-discovery`, `virtualenv` |
| `fqdn` | 1.5.1 | MPL-2.0 | File copyleft | cls | via `jsonschema` |
| `grimp` | 3.16 | BSD-2-Clause | Permissive | file | via `import-linter` |
| `hatchling` | 1.32.0 | MIT | Permissive | expr | direct |
| `humanize` | 4.16.0 | MIT | Permissive | expr | via `nox` |
| `hypothesis` | 6.167.0 | MPL-2.0 | File copyleft | expr | direct |
| `identify` | 2.6.19 | MIT | Permissive | meta | via `pre-commit` |
| `import-linter` | 2.14 | BSD-2-Clause | Permissive | file | direct |
| `iniconfig` | 2.3.0 | MIT | Permissive | expr | via `pytest` |
| `isoduration` | 20.11.0 | ISC | Permissive | cls | via `jsonschema` |
| `jsonpointer` | 3.1.1 | BSD-2-Clause | Permissive | file | via `jsonschema` |
| `jsonschema` | 4.23.0 | MIT | Permissive | cls | via `cyclonedx-python-lib` |
| `jsonschema-specifications` | 2025.9.1 | MIT | Permissive | expr | via `jsonschema` |
| `libcst` | 1.9.0 | MIT | Permissive | cls | via `mutmut` |
| `license-expression` | 30.4.4 | Apache-2.0 | Permissive | meta | via `cyclonedx-python-lib` |
| `mdit-py-plugins` | 0.6.1 | MIT | Permissive | cls | via `textual` |
| `msgpack` | 1.2.2 | Apache-2.0 | Permissive | expr | via `cachecontrol` |
| `mutmut` | 3.7.0 | BSD-3-Clause | Permissive | expr | direct |
| `mypy` | 1.14.1 | MIT | Permissive | cls | direct |
| `mypy-extensions` | 1.1.0 | MIT | Permissive | file | via `mypy` |
| `nodeenv` | 1.10.0 | BSD-2-Clause | Permissive | file | via `pre-commit` |
| `nox` | 2026.8.17 | Apache-2.0 | Permissive | expr | direct |
| `packageurl-python` | 0.17.6 | MIT | Permissive | cls | via `cyclonedx-bom`, `cyclonedx-python-lib` |
| `pathspec` | 1.1.1 | MPL-2.0 | File copyleft | cls | via `hatchling` |
| `pip` | 26.2.1 | MIT | Permissive | expr | via `pip-api` |
| `pip-api` | 0.0.34 | Apache-2.0 | Permissive | cls | via `pip-audit` |
| `pip-audit` | 2.10.1 | Apache-2.0 | Permissive | cls | direct |
| `pip-requirements-parser` | 32.0.1 | MIT | Permissive | meta | via `cyclonedx-bom`, `pip-audit` |
| `platformdirs` | 4.11.5 | MIT | Permissive | expr | via `nox`, `pip-audit`, `textual`… |
| `playwright` | 1.62.0 | Apache-2.0 | Permissive | expr | direct |
| `pre-commit` | 4.6.2 | MIT | Permissive | meta | direct |
| `py-serializable` | 2.1.0 | Apache-2.0 | Permissive | cls | via `cyclonedx-python-lib` |
| `pyee` | 13.0.1 | MIT | Permissive | cls | via `playwright` |
| `pyparsing` | 3.3.2 | MIT | Permissive | expr | via `pip-requirements-parser` |
| `pytest` | 9.1.1 | MIT | Permissive | expr | via `mutmut`, `pytest-asyncio`, `pytest-cov` |
| `pytest-asyncio` | 1.4.0 | Apache-2.0 | Permissive | expr | direct |
| `pytest-cov` | 7.1.0 | MIT | Permissive | expr | direct |
| `python-discovery` | 1.6.0 | MIT | Permissive | cls | via `nox`, `virtualenv` |
| `referencing` | 0.37.0 | MIT | Permissive | expr | via `cyclonedx-python-lib`, `jsonschema`, `jsonschema-specifications` |
| `rfc3339-validator` | 0.1.4 | MIT | Permissive | cls | via `jsonschema` |
| `rfc3987` | 1.3.8 | GPL-3.0-or-later | Strong copyleft | cls | via `jsonschema` |
| `rpds-py` | 2026.6.3 | MIT | Permissive | expr | via `jsonschema`, `referencing` |
| `ruff` | 0.9.10 | MIT | Permissive | cls | direct |
| `setproctitle` | 1.3.7 | BSD-3-Clause | Permissive | file | via `mutmut` |
| `sortedcontainers` | 2.4.0 | Apache-2.0 | Permissive | cls | via `cyclonedx-python-lib`, `hypothesis` |
| `stevedore` | 5.9.1 | Apache-2.0 | Permissive | expr | via `bandit` |
| `textual` | 8.2.8 | MIT | Permissive | cls | via `mutmut` |
| `tomli` | 2.4.1 | MIT | Permissive | expr | via `pip-audit` |
| `tomli-w` | 1.2.0 | MIT | Permissive | cls | via `pip-audit` |
| `tomlkit` | 0.15.1 | MIT | Permissive | cls | via `hatchling` |
| `trove-classifiers` | 2026.6.1.19 | Apache-2.0 | Permissive | cls | via `hatchling` |
| `uri-template` | 1.3.0 | MIT | Permissive | cls | via `jsonschema` |
| `virtualenv` | 21.7.7 | MIT | Permissive | expr | via `nox`, `pre-commit` |
| `webcolors` | 25.10.0 | BSD-3-Clause | Permissive | file | via `jsonschema` |

### The one GPL component in the graph

**`rfc3987` (GPL-3.0-or-later)** reaches the lockfile through
`pip-audit` → `cyclonedx-python-lib[validation]` → `jsonschema[format]`.
It is build-chain only and is never imported by the product, never installed
by a customer and never present in the wheel, so **no GPL obligation attaches
to Covenant Radar**. Two things still follow from it:

1. The SBOM at `dist/bom.json` is produced by `cyclonedx-py environment`,
   which describes the *build environment* rather than the *distributable*.
   A GPL-3.0 row therefore appears in released evidence for a component the
   product does not contain — a finding any downstream licence scan will
   raise.
2. `jsonschema` publishes `format-nongpl` as the drop-in, non-GPL alternative
   to `format`; upstream created it for exactly this situation.

Both are addressed in [§10](#10-findings-and-actions).

`chardet` (LGPL-2.1-or-later, via `cyclonedx-bom`), `hypothesis`, `pathspec`
and `fqdn` (all MPL-2.0) are likewise build-chain only and unmodified; notice
obligations only, and none of them ship.

---

## 7. Vendored assets

Assets copied into the tree are the components most easily missed by a
dependency scanner, because no package manager knows about them. Each is
pinned to a recorded version and a recorded digest, and each carries its
licence next to the bytes.

| Asset | Files | Version | Licence | Notice location |
|---|---|---|---|---|
| HTMX | `web/static/vendor/htmx/htmx.min.js` | 2.0.4 | MIT (© 2020 Big Sky Software) | `vendor/htmx/LICENSE`, `vendor/htmx/README.md` |
| Source Serif 4 | `web/static/fonts/radar-serif.ttf` | Google Fonts `ofl/sourceserif4` | OFL-1.1 (© Adobe Inc.) | `web/static/fonts/LICENSES.md` |
| IBM Plex Mono | `radar-mono-{regular,semibold,bold}.ttf` | Google Fonts `ofl/ibmplexmono` | OFL-1.1 (© IBM Corp.) | `web/static/fonts/LICENSES.md` |
| IBM Plex Sans | `radar-sans.ttf` | Google Fonts `ofl/ibmplexsans` | OFL-1.1 (© IBM Corp.) | `web/static/fonts/LICENSES.md` |
| Noto Sans Devanagari | `radar-devanagari.ttf` | Google Fonts `ofl/notosansdevanagari` | OFL-1.1 (© Google LLC) | `web/static/fonts/LICENSES.md` |

Why these are compliant as vendored copies:

- **Integrity is recorded.** `LICENSES.md` carries a SHA-256 for every font
  file and the HTMX record carries an SRI hash, so a release build can prove
  the registered bytes are the bytes shipped. A vendored asset that cannot be
  re-identified cannot be licence-cleared.
- **OFL-1.1 is satisfied.** The fonts are redistributed unmodified, bundled
  with software, not sold on their own, and the reserved font names are not
  used on the renamed files — the files are renamed but the register maps each
  one back to its upstream family and copyright holder, which is what the
  licence requires. OFL's copyleft reaches derivative *fonts*, never the
  software that embeds them.
- **MIT for HTMX** needs only the licence text, which is checked in verbatim.
- **Self-hosting is the reason they are vendored.** The CSP is
  `script-src 'self'` with no external origin, so no third-party CDN is
  contacted at runtime. That is a privacy and availability decision, but it is
  also what makes the licence position auditable: the bytes are in the tree.

No other vendored third-party code exists. `web/static/js/` is
first-party — `auth-scene.js` writes raw WebGL specifically to avoid taking
a three.js dependency, and the remaining scripts are dependency-free.

---

## 8. System and out-of-tree components

These are invoked, linked at runtime by the platform, or used in CI. **None
is redistributed by us**, which is what keeps their obligations off the
product.

| Component | Licence | How it is used | Position |
|---|---|---|---|
| Tesseract OCR | Apache-2.0 | Separate process, launched without a shell from `documents/ocr.py`; the executable is configured by `documents.ocr_command` | Permissive; supplied by the operator, not bundled |
| Ghostscript | **AGPL-3.0** (or a commercial Artifex licence) | Required by `ocrmypdf` — which the product never calls | Not invoked today. Separate-process use does not make our code a derivative work, but an AGPL prerequisite in a commercial deployment is a procurement question. Removing the unused dependency removes the question entirely ([§10](#10-findings-and-actions)) |
| Pango, GLib | LGPL-2.1-or-later | Loaded by WeasyPrint through `cffi` at PDF-render time | Platform packages, dynamically loaded, unmodified — LGPL obligations rest with whoever assembles the runtime image |
| cairo | LGPL-2.1-or-later or MPL-1.1 | Same | Same |
| HarfBuzz, fontconfig | MIT / MIT-style | Same | Permissive |
| PostgreSQL 17 server | PostgreSQL Licence (BSD-style) | Database; `postgres:17-alpine` in CI services | Permissive; not redistributed |
| libpq, OpenSSL | PostgreSQL Licence / Apache-2.0 | Bundled inside the `psycopg-binary` wheel | **Distributed** — noted in `THIRD_PARTY_NOTICES.md` |
| Chromium | BSD-3-Clause and others | Playwright browser for `e2e` and `a11y` suites | Test-only, never shipped |
| `actions/checkout`, `actions/setup-python`, `actions/upload-artifact` | MIT | CI and release workflows | Not redistributed |

A deployment image therefore needs its own notice file for the LGPL system
libraries it installs. That obligation belongs to the image, not to this
repository, and it is recorded here so it is not lost between the two.

---

## 9. Data and model IP

Licence compliance is only half of IP compliance. The other half is whether
the product embeds anyone else's data, and whether anything it sends out
creates a claim against the customer.

**No third-party corpus ships.** The demo and reference portfolios are
generated in-repo from a SHA-256 seed —
`evaluation/reference_portfolio/`, `src/covenant_radar/demo/` — never from
process randomness, a wall clock, a scraped source or a customer extract. The
same seed reproduces the same 5,000-borrower book byte for byte, so the data
is provably synthetic and provably ours.

**No pre-trained third-party weights ship.** The Stage-4 challenger models are
trained at build time by `evaluation/ml_reference.py` from that synthetic
portfolio, using scikit-learn (BSD-3-Clause) estimators. The artefact path in
`.env.example` points at a locally produced `.pkl`. Nothing is downloaded from
a model hub, so no model licence or dataset licence attaches.

**Signals are synthetic until a subscription exists.** External signal
ingestion runs against a documented synthetic feed adapter precisely so the
feed, entity-resolution and review-queue paths can be exercised before any
licensed news or bureau subscription is procured. Substituting a real feed is
a customer procurement decision carrying that vendor's own terms; nothing in
this repository presumes one.

**Model providers are customer-supplied.** `ai/providers/` adapts Anthropic,
Azure OpenAI, a TCS GenAI Lab endpoint and an offline recorded-response
provider. The adapters are first-party HTTP clients over published APIs — no
vendor SDK is taken as a dependency, so no vendor SDK licence applies. The
commercial terms of whichever endpoint a customer configures are that
customer's contract. Two controls keep our side of it clean: every outbound
call passes one guarded, fail-closed masking site, and the recorded provider
lets the whole system run and be demonstrated with no third-party call at all.

**Prompts and model cards are first-party.** `ai/prompts/` and
`docs/model-cards/` are original work under the repository's MIT licence.

---

## 10. Findings and actions

Everything above is the position as it stands. Four items are worth acting on.
None is a licence violation; the first two remove obligations the product is
carrying for no benefit.

### F-1 — Six declared runtime dependencies are never imported

`authlib`, `ocrmypdf`, `pandas`, `pydantic-settings`, `pysaml2` and `tenacity`
appear in `[project].dependencies` but are imported by no module in `src/` or
`evaluation/`. (`psycopg` and `python-multipart` are *also* never imported but
are genuine runtime dependencies — SQLAlchemy loads the first from the
connection URL and Starlette's `request.form()` needs the second. They stay.)

The functionality each was presumably added for is hand-written instead:
`security/oidc.py` implements the OIDC authorization-code flow on `httpx`,
`security/saml.py` validates SAML assertions on `lxml` and `cryptography`, and
`config/settings.py` builds settings on plain `pydantic`.

Every declared dependency is a licence obligation, an SBOM row and a
`pip-audit` surface. Dropping these six removes `img2pdf` (LGPL-3.0),
`pi-heif`/libheif (LGPL-3.0), `pikepdf` (MPL-2.0), `xmlschema`, `pyopenssl`
and `defusedxml` from the shipped graph, and removes the product's only
AGPL system prerequisite.

**Action:** confirm against the roadmap that none is reserved for imminent
work, then remove them from `pyproject.toml`, recompile `requirements.lock`,
and correct the stack table in `README.md` §7, which currently lists
`authlib`, `pysaml2`, `ocrmypdf`, `pandas`, `pydantic-settings` and `tenacity`
as though they were in use.

### F-2 — The released SBOM describes the build environment, not the product

`release.yml` runs `cyclonedx-py environment`, so `dist/bom.json` inventories
everything installed in the release job — all 170 locked packages, including
the 71 that never ship and the GPL-3.0 `rfc3987` among them. A downstream
scanner reading that SBOM will conclude the product contains GPL-3.0 code. It
does not.

**Action:** emit two boms — `cyclonedx-py environment` kept as build-chain
evidence, plus a distributable BOM generated from the built wheel or from the
runtime-only requirement set — and attach both. Additionally, pin
`jsonschema[format-nongpl]` (upstream's own non-GPL alternative) so `rfc3987`
leaves the lockfile entirely.

### F-3 — Licence review is not yet an automated gate

`security.yml` gates vulnerabilities (`pip-audit`), source findings (`bandit`)
and secrets (`detect-secrets`). Nothing fails a build when a dependency
changes licence or a new copyleft component enters the graph — today that is
caught only by a human reading this document.

**Action:** add a licence step to `security.yml` that resolves each locked
distribution's licence and fails on any licence outside the allowed set in
[§3](#3-licence-policy). `cyclonedx-py` is already a dev dependency and its
BOM carries per-component licence data, so the check can run off the artefact
that is already produced rather than a new tool.

### F-4 — Deployment-image notices are out of scope here

The LGPL system libraries WeasyPrint binds ([§8](#8-system-and-out-of-tree-components))
carry notice and source-offer obligations for whoever builds the runtime
image. This repository cannot discharge them because it does not build that
image.

**Action:** whichever artefact installs Pango, cairo and GLib carries its own
notice file. Recorded here so the obligation is not dropped in the gap between
the two.

---

## 11. Maintenance

**When a dependency is added or changed**

1. Declare it in `pyproject.toml` in the correct group — shipping or dev.
2. Recompile: `uv pip compile pyproject.toml --all-extras --output-file requirements.lock`.
3. Diff the lockfile. For every added package, resolve its licence from its
   own metadata; where metadata is not an SPDX expression, read the upstream
   licence file rather than trusting the classifier.
4. Classify against [§3](#3-licence-policy). Anything outside **Permissive**
   needs a written position in this document before merge. Anything in
   **Strong copyleft** does not enter the distributable.
5. Add the row to [§4](#4-distributed-runtime-components),
   [§5](#5-transitive-runtime-components) or
   [§6](#6-build-and-test-only-components), and add the notice to
   [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) if it ships.

**When a vendored asset is refreshed** — replace the bytes, update the version
*and the digest* in `vendor/htmx/README.md` or `web/static/fonts/LICENSES.md`,
and re-check the licence: an upstream relicence between versions is exactly
what a digest-pinned register is for.

**At every release** — the evidence pack (wheel, sdist, SBOM) is built and
attached by `release.yml`. This register and `THIRD_PARTY_NOTICES.md` ship in
the source tree and should be reviewed as part of the release checklist, not
after it.

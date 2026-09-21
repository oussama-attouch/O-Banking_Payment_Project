# Third-party notices

O-Banking vendors its front-end assets directly into `static/`. Nothing is loaded
from a CDN at runtime, so the application runs fully offline.

This file records what was removed from the project as received, what was added
in its place, and the licence that covers the added code.

---

## Removed

The project was imported as a commercial "Paylio" template and is being migrated
to Tabler. The following are removed by that migration.

| Item | Where it lived | Why it is removed |
| --- | --- | --- |
| **Paylio — Money Transfer and Online Payments Dashboard HTML Template** | `static/assets/` (130 files, 6.30 MB) and `static/assets1/` (76 files, 5.52 MB) | Commercial template redistributed without a licence. **The tarball as received contained no `LICENSE`, `README`, `COPYING` or purchase record of any kind.** The template header in `css/style.css` names it "Paylio Admin Dashboard"; the vendor domains `pixner.net` and `softalab.com` appear in the shipped files. |
| **`counter.js` — jQuery Counter-Up 1.0** | `static/assets/js/plugin/counter.js` | **GPL v2** (`Copyright 2013, Benjamin Intal`, "Released under the GPL v2 License"). Copyleft, and incompatible with redistributing the rest of the bundle. |
| **Font Awesome** | `static/assets/css/fontawesome.min.css`, `static/assets1/css/fontawesome.min.css`, `static/assets/js/fontawesome.js`, `static/assets/webfonts/*`, `static/assets1/webfonts/*` | Superseded by Tabler Icons; the `assets/` webfont copies are not fonts at all (see below). |
| **"arafat font" custom icon font** | `static/{assets,assets1}/css/arafat-font.css` + `webfonts/arafat-font.*` | A Glyphter-generated icon font whose `.eot`, `.ttf` and `.svg` variants were never shipped and whose `.woff` is a 404 capture. It could not render, so every `icon-*` class was a blank glyph. |
| **Vendor-mirror 404 captures** | `static/assets/css/plugin/apexcharts.css` (87 B), `static/assets1/css/plugin/apexcharts.css` (88 B), `static/assets/webfonts/arafat-font.woff` (87 B), `static/assets/webfonts/fa-brands-400.woff2` (83 B), `static/assets/webfonts/fa-solid-900.woff2` (89 B) | These files are not CSS or fonts. Each contains a single plain-text line such as `No Content: https://pixner.net/paylio/paylio-dashboard/assets/css/plugin/apexcharts.css` — the saved body of a failed download from a theme-mirror domain. They have been deleted rather than shipped. |

> **Status.** The Paylio asset trees are still present on disk at the time of
> writing; deleting them is the final step of the front-end migration. The
> remaining items are removed as each page stops referencing them.

`static/assets/js/plugin/counter.js` and the five 404-capture files listed above
are **already unreferenced** by any template, and no shippable copy of the Paylio
theme goes into the public repository's release artefacts.

---

## Added

| Package | Version | Licence | Source |
| --- | --- | --- | --- |
| [`@tabler/core`](https://www.npmjs.com/package/@tabler/core) | 1.5.1 | MIT | `https://registry.npmjs.org/@tabler/core/-/core-1.5.1.tgz` |
| [`@tabler/icons-webfont`](https://www.npmjs.com/package/@tabler/icons-webfont) | 3.47.0 | MIT | `https://registry.npmjs.org/@tabler/icons-webfont/-/icons-webfont-3.47.0.tgz` |

Tabler is published to the npm registry. Its GitHub releases carry **no prebuilt
binary assets**, which is why the source of record here is the npm tarball rather
than a release archive.

### Vendored files

The subset below is what `static/tabler/` contains. Source maps, RTL variants, the
unminified `tabler.css` / `tabler.js`, `dist/libs/*`, `dist/img/*`, the SCSS
sources and `package.json` are deliberately **not** vendored.

| File | Size | Origin |
| --- | --- | --- |
| `static/tabler/css/tabler.min.css` | 677.5 KB | `@tabler/core` `dist/css/` |
| `static/tabler/css/tabler-vendors.min.css` | 12.0 KB | `@tabler/core` `dist/css/` |
| `static/tabler/js/tabler.min.js` | 83.4 KB | `@tabler/core` `dist/js/` |
| `static/tabler/icons/tabler-icons.min.css` | 206.8 KB | `@tabler/icons-webfont` `dist/` |
| `static/tabler/icons/fonts/tabler-icons.woff2` | 490.8 KB | `@tabler/icons-webfont` `dist/fonts/` |
| `static/tabler/icons/fonts/tabler-icons.woff` | 755.9 KB | `@tabler/icons-webfont` `dist/fonts/` |
| `static/tabler/icons/LICENSE` | 1.0 KB | `@tabler/icons-webfont` |
| `static/tabler/LICENSE` | 1.0 KB | see note below |

No modifications were made to any vendored file; each is a byte-for-byte copy of
its upstream original.

**Note on `static/tabler/LICENSE`.** The `@tabler/core` npm tarball does **not**
contain a `LICENSE` file — its payload is `dist/`, `js/`, `scss/`, `libs.json`,
`package.json` and `README.md`. Its MIT licence is declared in `package.json`
(`"license": "MIT"`, `"author": "codecalm"`, repository
`github.com/tabler/tabler`) and in the banner comment at the top of
`tabler.min.css`:

```
/*!
 * Tabler v1.5.1 (https://tabler.io)
 * Copyright 2018-2026 The Tabler Authors
 * Copyright 2018-2026 codecalm.net Paweł Kuna
 * Licensed under MIT (https://github.com/tabler/tabler/blob/master/LICENSE)
 */
```

`static/tabler/LICENSE` is the Tabler project's MIT licence text as shipped in the
sibling `@tabler/icons-webfont` package. Both packages are MIT, both are the work
of the same authors, and the text is identical.

### Offline guarantee

The vendored assets make **zero** network requests. Verified against the shipped
files:

- `tabler.min.css` contains **0** `@font-face` rules and **0** non-`data:` `url()`
  references. Typography resolves through CSS custom properties onto a system font
  stack, so no webfont is fetched.
- No reference to `fonts.googleapis.com` or `fonts.gstatic.com` exists anywhere in
  the vendored CSS.
- `tabler-icons.min.css` references its fonts through the relative paths
  `./fonts/tabler-icons.woff2` and `./fonts/tabler-icons.woff`, which resolve
  inside `static/tabler/icons/`.

---

## Licence text

MIT License

Copyright (c) 2020-2026 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## About this project

O-Banking is a personal, academic reimplementation of a banking and payments
application, written as a final-year (PFA) project. It is not affiliated with,
endorsed by, or derived from the commercial "Paylio" template's authors, and it is
not a product of Tabler or its maintainers. The application logic — the Django
models, views, forms, migrations and tests — is original work. The visual layer
uses Tabler, an open-source MIT-licensed dashboard framework, as a starting point
for markup and styling, and no part of the original commercial theme is
redistributed in this repository once the migration is complete.

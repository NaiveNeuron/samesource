#!/usr/bin/env python3
"""Build a self-contained labelling page for the human link audit.

Reads evidence/scale/audit-sheet.json and writes output/audit/label.html, a
single offline file. It shows one sampled link at a time, never the model
raters' labels, keeps progress in the browser per rater name, and exports
audit-human-<rater>.csv for scripts/scale/audit_agreement.py.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
SHEET = PROJECT / "evidence" / "scale" / "audit-sheet.json"
OUT = PROJECT / "output" / "audit" / "label.html"

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SameSource link audit</title>
<style>
:root {
  --paper: #e8ede9; --sheet: #fbfcfa; --ink: #16201b; --quiet: #5b6a62; --rule: #c5cfc8;
  --deb: #a80030; --rpm: #2f5fa8; --pkgbuild: #0f7fb5; --ebuild: #54487a;
  --same: #1f7a4d; --part: #a8690a; --vend: #7a4fa3; --diff: #b3261e; --unsure: #5b6a62;
  --sans: "Avenir Next", "Seravek", "Segoe UI", system-ui, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.45 var(--sans); }
button, input { font: inherit; color: inherit; }
:focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
a { color: inherit; }

header { display: flex; gap: 1rem; align-items: baseline; flex-wrap: wrap;
  padding: .9rem 1.4rem .5rem; }
header h1 { font-size: 1.05rem; margin: 0; font-weight: 650; }
header .count { color: var(--quiet); font-variant-numeric: tabular-nums; }
header .spacer { flex: 1; }
header label { color: var(--quiet); font-size: .9rem; }
header input { width: 9rem; padding: .25rem .5rem; border: 1px solid var(--rule);
  border-radius: 6px; background: var(--sheet); }
.plain { background: none; border: 1px solid var(--rule); border-radius: 6px;
  padding: .25rem .7rem; cursor: pointer; }
.plain:hover { background: var(--sheet); }

/* The strip: one cell per sampled link, coloured by verdict. */
#strip { display: grid; grid-template-columns: repeat(60, 1fr); gap: 2px; padding: 0 1.4rem .8rem; }
#strip button { height: 13px; border: 0; padding: 0; border-radius: 2px; background: var(--rule);
  cursor: pointer; }
#strip button[data-v="same_primary_archive"] { background: var(--same); }
#strip button[data-v="component_or_split"] { background: var(--part); }
#strip button[data-v="vendored_or_bundled"] { background: var(--vend); }
#strip button[data-v="different_product"] { background: var(--diff); }
#strip button[data-v="unclear"] { background: var(--unsure); }
#strip button.here { outline: 2px solid var(--ink); outline-offset: 1px; }
#strip button.pairstart { margin-left: 4px; }

main { max-width: 1180px; margin: 0 auto; padding: 0 1.4rem 9.5rem; }
.case { display: grid; grid-template-columns: 1fr 1fr; gap: 0 1.2rem; }
.side { background: var(--sheet); border-radius: 10px; padding: 1rem 1.2rem 1.1rem;
  border-top: 6px solid var(--fam); }
.side .distro { color: var(--fam); font-weight: 650; }
  .side .pkg { font: 600 1.35rem/1.2 var(--mono); margin: .15rem 0 .1rem; overflow-wrap: anywhere; }
.side .ver { font-family: var(--mono); color: var(--quiet); }
.side dl { margin: .9rem 0 0; display: grid; grid-template-columns: auto 1fr; gap: .25rem .8rem;
  font-size: .92rem; }
.side dt { color: var(--quiet); }
.side dd { margin: 0; overflow-wrap: anywhere; }
.side dd.mono { font-family: var(--mono); font-size: .86rem; }
.side .open { display: inline-block; margin-top: .9rem; padding: .35rem .8rem; border-radius: 6px;
  background: var(--fam); color: #fff; text-decoration: none; font-weight: 600; }

.side h3 { font-size: .92rem; margin: 1rem 0 .3rem; font-weight: 650; }
.side ul.arts { list-style: none; margin: 0; padding: 0; font: .84rem/1.5 var(--mono); }
.side ul.arts li { display: flex; gap: .6rem; overflow-wrap: anywhere; }
.side ul.arts .role { flex: 0 0 6.2rem; color: var(--quiet); font-family: var(--sans); }
.side ul.arts li.linked { background: color-mix(in srgb, var(--fam) 14%, transparent); font-weight: 600; }
.side .bins { font: .84rem/1.5 var(--mono); overflow-wrap: anywhere; }
.side pre { margin: 0; max-height: 46vh; overflow: auto; background: #f1f4f1; border-radius: 6px;
  padding: .6rem .7rem; font: .8rem/1.45 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.side pre mark { background: color-mix(in srgb, var(--fam) 28%, transparent); color: inherit; }
.side pre .key { background: #fff3c4; }
.side .note { color: var(--quiet); font-size: .85rem; }
/* The shared archive joins the two sides. */
.join { grid-column: 1 / -1; grid-row: 2; background: var(--ink); color: var(--sheet);
  border-radius: 10px; margin: .6rem 0; padding: .7rem 1.2rem; display: flex; gap: 1.2rem;
  align-items: baseline; flex-wrap: wrap; }
.join strong { font-weight: 600; }
.join .algs { font-family: var(--mono); font-size: .86rem; opacity: .85; }
.flag { margin-top: .8rem; padding: .5rem .8rem; border-left: 4px solid var(--part);
  background: var(--sheet); border-radius: 0 6px 6px 0; }

footer { position: fixed; left: 0; right: 0; bottom: 0; background: var(--sheet);
  border-top: 1px solid var(--rule); padding: .7rem 1.4rem .8rem; }
.verdicts { display: grid; grid-template-columns: repeat(5, 1fr); gap: .5rem; max-width: 1180px;
  margin: 0 auto; }
.verdicts button { text-align: left; padding: .5rem .7rem; border-radius: 8px; cursor: pointer;
  border: 2px solid var(--c); background: var(--sheet); line-height: 1.25; }
.verdicts button b { display: block; }
.verdicts button span { color: var(--quiet); font-size: .82rem; }
.verdicts button kbd { float: right; font: 600 .85rem var(--mono); color: var(--c); }
.verdicts button[aria-pressed="true"] { background: var(--c); color: #fff; }
.verdicts button[aria-pressed="true"] span, .verdicts button[aria-pressed="true"] kbd { color: #fff; }
.under { max-width: 1180px; margin: .55rem auto 0; display: flex; gap: .8rem; align-items: center; }
.under input { flex: 1; padding: .35rem .6rem; border: 1px solid var(--rule); border-radius: 6px;
  background: #fff; }
.under .keys { color: var(--quiet); font-size: .85rem; white-space: nowrap; }
kbd { font-family: var(--mono); }
#done { display: none; margin: 1rem 0; padding: .8rem 1rem; background: var(--same); color: #fff;
  border-radius: 8px; }
@media (max-width: 1100px), (max-height: 800px) { .verdicts button span { display: none; } }
@media (max-width: 760px) {
  .case { grid-template-columns: 1fr; } .side { margin-bottom: .6rem; grid-row: auto !important; } .join { grid-row: auto; }
  .join { border-radius: 10px; } .verdicts { grid-template-columns: 1fr 1fr; }
  #strip { grid-template-columns: repeat(30, 1fr); } main { padding-bottom: 19rem; }
  .under .keys { display: none; }
}
</style>
</head>
<body>
<header>
  <h1>Do these two packages build their main program from this archive?</h1>
  <span class="count" id="count"></span>
  <span class="spacer"></span>
  <label>Rater <input id="rater" placeholder="your initials" autocomplete="off"></label>
  <button class="plain" id="import">Load CSV</button>
  <button class="plain" id="export">Save CSV</button>
  <input type="file" id="file" accept=".csv" hidden>
</header>
<nav id="strip" aria-label="All sampled links"></nav>
<main>
  <div id="done">All 180 labelled. Save the CSV and put it in evidence/scale/.</div>
  <section class="case" id="case"></section>
  <div id="flag"></div>
</main>
<footer>
  <div class="verdicts" id="verdicts"></div>
  <div class="under">
    <input id="reason" placeholder="Reason, optional (press R)" autocomplete="off">
    <span class="keys"><kbd>1</kbd>–<kbd>5</kbd> label and go on · <kbd>←</kbd><kbd>→</kbd> move ·
      <kbd>U</kbd> next unlabelled · <kbd>O</kbd> open both recipes</span>
  </div>
</footer>
<script>
const ITEMS = __ITEMS__;
const VERDICTS = [
  ["same_primary_archive", "Same main source", "Both build their own main program from it", "--same"],
  ["component_or_split", "Part or rebuild", "Sub-project, docs/data split, MinGW or cross build", "--part"],
  ["vendored_or_bundled", "Vendored", "For one side it is a bundled dependency", "--vend"],
  ["different_product", "Different software", "Unrelated packages sharing a file", "--diff"],
  ["unclear", "Can't tell", "Needs a closer look later", "--unsure"],
];
const DISTRO = {deb: "Debian", rpm: "Fedora", pkgbuild: "Arch Linux", ebuild: "Gentoo"};
const $ = (id) => document.getElementById(id);
let at = 0, labels = {};

const key = () => "samesource-audit:" + ($("rater").value.trim() || "anonymous");
const load = () => { labels = JSON.parse(localStorage.getItem(key()) || "{}"); };
const save = () => localStorage.setItem(key(), JSON.stringify(labels));

function recipeUrl(s) {
  const p = s.packaging, repo = (p.repository || "").replace(/\.git$/, "");
  if (s.family === "deb")
    return "https://sources.debian.org/src/" + s.source_package + "/" +
           encodeURIComponent(s.version.replace(/^\d+:/, "")) + "/debian/";
  if (s.family === "rpm") return repo + "/blob/" + p.commit + "/f/" + s.source_package + ".spec";
  if (s.family === "pkgbuild") return repo + "/-/blob/" + p.commit + "/PKGBUILD";
  return repo + "/blob/" + p.commit + "/" + p.path;
}
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const bare = (n) => n.split("/").pop().toLowerCase().replace(/[-_.]/g, "");

const KEYLINE = /^\s*(S=|Source\d*:|source(_\w+)?=|SRC_URI|%build|%prep|build\(\)|package(_[\w-]+)?\(\)|Package:|Summary:|DESCRIPTION=|pkgdesc=|Description:|override_dh_auto|cd |LLVM_COMPONENTS|CMAKE_USE_DIR)/;
function recipeHtml(s) {
  const stem = s.artifact.replace(/\.(tar\.\w+|tgz|zip|crate|gem)$/, "");
  return s.recipe.map((f) => {
    const body = f.text.split("\n").map((line) => {
      const e = esc(line);
      if (line.includes(s.artifact) || line.includes(stem)) return "<mark>" + e + "</mark>";
      return KEYLINE.test(line) ? '<span class="key">' + e + "</span>" : e;
    }).join("\n");
    return `<h3>${esc(f.name)}${f.note ? ` <span class="note">(${esc(f.note)})</span>` : ""}</h3>` +
           (f.text ? `<pre>${body}</pre>` : "");
  }).join("");
}
function side(s) {
  return `<article class="side" style="--fam: var(--${s.family}); border-top: 6px solid var(--fam)">
    <div class="distro">${DISTRO[s.family]}</div>
    <div class="pkg">${esc(s.source_package)}</div>
    <div class="ver">${esc(s.version)}</div>
    <dl><dt>Downloads</dt><dd class="mono">${esc(s.artifact)}</dd>
        <dt>From</dt><dd class="mono">${esc(s.url || "no URL in the recipe")}</dd>
        <dt>Picked because</dt><dd>${esc(s.role_evidence)}</dd></dl>
    <a class="open" href="${esc(recipeUrl(s))}" target="_blank" rel="noopener">Open the recipe</a>
  </article>`;
}
function evidence(s) {
  const arts = s.artifacts.map((a) =>
    `<li class="${a.name === s.artifact ? "linked" : ""}"><span class="role">${esc(a.role)}</span>${esc(a.name)}</li>`).join("");
  const more = s.n_artifacts > s.artifacts.length ? `<p class="note">and ${s.n_artifacts - s.artifacts.length} more</p>` : "";
  const bins = s.binaries.length ? `<h3>Builds these packages</h3><div class="bins">${esc(s.binaries.join(", "))}</div>` : "";
  return `<article class="side" style="--fam: var(--${s.family}); grid-row: 3">
    <h3 style="margin-top:0">Everything this recipe downloads (${s.n_artifacts})</h3>
    <ul class="arts">${arts}</ul>${more}${bins}${recipeHtml(s)}
  </article>`;
}

function render() {
  const it = ITEMS[at], [a, b] = it.sides;
  $("case").innerHTML = side(a) + side(b) + evidence(a) + evidence(b) +
    `<div class="join"><strong>Byte-identical archive</strong>
     <span class="algs">matched on ${esc(it.algorithms.join(", "))}</span></div>`;
  const notes = [];
  if (bare(a.source_package) !== bare(b.source_package)) notes.push("The package names differ.");
  if (/mingw|cross|riscv|arm-none|avr-/i.test(a.source_package + b.source_package))
    notes.push("One name suggests a cross or MinGW build.");
  $("flag").innerHTML = notes.length ? `<p class="flag">${notes.join(" ")}</p>` : "";
  const cur = labels[at] || {};
  document.querySelectorAll("#verdicts button").forEach((btn) =>
    btn.setAttribute("aria-pressed", btn.dataset.v === cur.verdict));
  $("reason").value = cur.reason || "";
  document.querySelectorAll("#strip button").forEach((c, i) => {
    c.dataset.v = (labels[i] || {}).verdict || "";
    c.classList.toggle("here", i === at);
  });
  const n = Object.values(labels).filter((l) => l.verdict).length;
  $("count").textContent = `Link ${at + 1} of ${ITEMS.length} · ${n} labelled`;
  $("done").style.display = n === ITEMS.length ? "block" : "none";
}

function go(i) { at = Math.max(0, Math.min(ITEMS.length - 1, i)); render(); }
function nextOpen() {
  for (let k = 1; k <= ITEMS.length; k++) {
    const i = (at + k + ITEMS.length) % ITEMS.length;
    if (!(labels[i] || {}).verdict) return go(i);
  }
  go(at);
}
function label(v) {
  labels[at] = {verdict: v, reason: $("reason").value.trim()};
  save();
  at < ITEMS.length - 1 ? go(at + 1) : render();
}

function csv() {
  const q = (t) => '"' + String(t ?? "").replace(/"/g, '""') + '"';
  const rows = [["n", "pair", "link_a", "link_b", "verdict", "reason"].join(",")];
  ITEMS.forEach((it, i) => {
    const l = labels[i] || {};
    rows.push([i + 1, it.pair, q(it.link[0]), q(it.link[1]), l.verdict || "", q(l.reason || "")].join(","));
  });
  const name = "audit-human-" + ($("rater").value.trim() || "anonymous") + ".csv";
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(new Blob([rows.join("\n") + "\n"], {type: "text/csv"})), download: name});
  a.click();
}
function parseCsv(text) {
  const out = [];
  for (const line of text.trim().split(/\r?\n/).slice(1)) {
    const cells = []; let cur = "", inq = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inq) { if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') inq = false; else cur += c; }
      else if (c === '"') inq = true; else if (c === ",") { cells.push(cur); cur = ""; } else cur += c;
    }
    cells.push(cur); out.push(cells);
  }
  return out;
}

$("verdicts").innerHTML = VERDICTS.map(([v, name, hint, c], i) =>
  `<button data-v="${v}" style="--c: var(${c})" aria-pressed="false"><kbd>${i + 1}</kbd><b>${name}</b><span>${hint}</span></button>`).join("");
$("verdicts").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) label(b.dataset.v); });
$("strip").innerHTML = ITEMS.map((it, i) =>
  `<button title="${i + 1}: ${esc(it.pair)}" class="${i && it.pair !== ITEMS[i - 1].pair ? "pairstart" : ""}"></button>`).join("");
$("strip").addEventListener("click", (e) => {
  const i = [...$("strip").children].indexOf(e.target); if (i >= 0) go(i); });
$("reason").addEventListener("change", () => {
  if (labels[at]) { labels[at].reason = $("reason").value.trim(); save(); } });
$("rater").value = localStorage.getItem("samesource-audit-rater") || "";
$("rater").addEventListener("change", () => {
  // Typing a name after labelling keeps the labels: they move to the new name
  // unless that name already has its own.
  const carried = labels;
  localStorage.setItem("samesource-audit-rater", $("rater").value.trim()); load();
  if (!Object.keys(labels).length && Object.keys(carried).length) { labels = carried; save(); }
  at = -1; nextOpen(); });
$("export").addEventListener("click", csv);
$("import").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", async (e) => {
  for (const c of parseCsv(await e.target.files[0].text()))
    if (c[4]) labels[Number(c[0]) - 1] = {verdict: c[4], reason: c[5] || ""};
  save(); render();
});
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") { if (e.key === "Escape" || e.key === "Enter") e.target.blur(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key >= "1" && e.key <= "5") label(VERDICTS[Number(e.key) - 1][0]);
  else if (e.key === "ArrowRight" || e.key === "j") go(at + 1);
  else if (e.key === "ArrowLeft" || e.key === "k") go(at - 1);
  else if (e.key === "u") nextOpen();
  else if (e.key === "r") { e.preventDefault(); $("reason").focus(); }
  else if (e.key === "o") document.querySelectorAll(".side .open").forEach((a) => window.open(a.href, "_blank"));
});
load();
// Recover labels saved under another rater name (e.g. before a name was typed).
if (!Object.keys(labels).length) {
  let best = null;
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (!k.startsWith("samesource-audit:")) continue;
    const n = Object.keys(JSON.parse(localStorage.getItem(k) || "{}")).length;
    if (n && (!best || n > best.n)) best = {k, n};
  }
  if (best && confirm(`Found ${best.n} labels saved under "${best.k.split(":")[1]}". Use them for "${$("rater").value.trim() || "anonymous"}"?`)) {
    labels = JSON.parse(localStorage.getItem(best.k)); save();
  }
}
at = -1; nextOpen();
</script>
</body>
</html>
"""


RAW = Path.home() / ".cache" / "pkgbridge" / "scale"
MAX_LINES = 400


def recipe_text(side: dict) -> list[dict]:
    """Recipe files for one side, from the caches the dataset scripts filled.

    Returns [{"name", "text"}]; an entry with empty text and a note when the
    file is not cached, so the page says so instead of showing nothing.
    """
    fam, name, p = side["family"], side["source_package"], side["packaging"]
    try:
        if fam == "deb":
            version = side["version"].split(":", 1)[-1]
            tar = next((RAW / "raw" / "patches" / "deb").glob(f"{name}_{version}.debian.tar.*"))
            out = []
            with tarfile.open(tar) as t:
                members = {m.name.removeprefix("./"): m for m in t.getmembers()}
                for f in ("debian/control", "debian/rules"):
                    if f in members:
                        out.append({"name": f, "text": t.extractfile(members[f]).read().decode(errors="replace")})
            return out
        if fam == "rpm":
            path = RAW / "raw" / "patches" / "rpm" / f"{name}-{p['commit']}.spec"
            return [{"name": f"{name}.spec", "text": path.read_text(errors="replace")}]
        if fam == "ebuild":
            path = RAW / "gentoo-mirror" / p["path"]
            return [{"name": Path(p["path"]).name, "text": path.read_text(errors="replace")}]
        cache = RAW / "raw" / "audit-ui" / f"{name}-{p['commit']}.PKGBUILD"
        if not cache.exists():
            url = f"{p['repository'].removesuffix('.git')}/-/raw/{p['commit']}/PKGBUILD"
            req = urllib.request.Request(url, headers={"User-Agent": "samesource-audit-ui"})
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        return [{"name": "PKGBUILD", "text": cache.read_text(errors="replace")}]
    except Exception as exc:  # noqa: BLE001 - show the gap on the page
        return [{"name": "recipe not cached", "text": "", "note": f"{type(exc).__name__}: open the recipe link instead"}]


def trim(files: list[dict]) -> list[dict]:
    for f in files:
        lines = f["text"].splitlines()
        if len(lines) > MAX_LINES:
            f["text"] = "\n".join(lines[:MAX_LINES])
            f["note"] = f"first {MAX_LINES} of {len(lines)} lines"
    return files


def main() -> None:
    sheet = json.loads(SHEET.read_text())
    wanted = {s["record"] for it in sheet["items"] for s in it["sides"]}
    records = {}
    for fam in ("deb", "rpm", "pkgbuild", "ebuild"):
        for line in (PROJECT / "evidence" / "scale" / f"{fam}.jsonl").open():
            r = json.loads(line)
            if r["id"] in wanted:
                records[r["id"]] = r
    def side(s: dict) -> dict:
        rec = records.get(s["record"], {})
        return ({k: s[k] for k in ("family", "source_package", "version", "artifact", "url", "role_evidence")}
                | {"packaging": {k: s["packaging"].get(k) for k in ("repository", "commit", "path")},
                   "binaries": rec.get("binary_packages") or [],
                   "artifacts": [{"name": a.get("name"), "role": a.get("role"), "kind": a.get("kind")}
                                 for a in rec.get("artifacts", [])][:40],
                   "n_artifacts": len(rec.get("artifacts", [])),
                   "recipe": trim(recipe_text(s))})

    items = [{"pair": it["pair"], "link": it["link"], "algorithms": it["algorithms"],
              "sides": [side(s) for s in it["sides"]]}
             for it in sheet["items"]]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(PAGE.replace("__ITEMS__", json.dumps(items).replace("</", "<\\/")))
    print(f"wrote {OUT} ({len(items)} links)")


if __name__ == "__main__":
    main()

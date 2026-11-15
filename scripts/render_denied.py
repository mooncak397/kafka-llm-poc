#!/usr/bin/env python3
"""Render evidence/negative-test.txt (Table 3 + key lines) to HTML, then PNG via shot.sh. Usage: render_denied.py OUT.png"""
import html, re, subprocess, sys, tempfile, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
txt = (root / "evidence/negative-test.txt").read_text().splitlines()
head = txt[1]
req = txt[2]
rows = [[c.strip() for c in l.strip("|").split("|")] for l in txt if re.match(r"\| \(", l)]
keys = [l for l in txt if re.match(r"\([bc]\) (broker|Error|ERROR|topics)", l)]
keys = [re.sub(r" from host = \S+", "", l) for l in keys]  # drop pod IP
e = html.escape
cards = "".join(
    f"<div class=c><div class=t>{e(r[0])}</div><dl><dt>Tool call</dt><dd>{e(r[2])}</dd>"
    f"<dt>Kafka answer</dt><dd>{e(r[3])}</dd></dl>"
    f"<div class=s>Topic still there: <b>{e(r[4]).upper()}</b></div></div>" for r in rows)
page = f"""<meta charset=utf-8><style>
body{{margin:0;background:#0f1117;color:#d8dbe2;font:22px/1.45 'DejaVu Sans',sans-serif;padding:40px 48px;width:1504px}}
h1{{font-size:34px;margin:0 0 6px}} .m{{color:#9aa0ad;font-size:19px}} .q{{margin:22px 0;padding:14px 20px;background:#1a1d27;border-left:6px solid #5794f2}}
.c{{background:#161923;border:1px solid #2a2f3d;border-radius:8px;padding:18px 24px;margin:0 0 16px}}
.t{{font-weight:bold;font-size:24px;color:#f2cc0c;margin-bottom:6px}} dl{{margin:0;display:grid;grid-template-columns:150px 1fr;gap:4px 16px}}
dt{{color:#9aa0ad}} dd{{margin:0;font-family:'DejaVu Sans Mono',monospace;font-size:18px;word-break:break-word}}
.s{{margin-top:8px}} .s b{{color:#73bf69}} pre{{background:#0b0d12;border:1px solid #2a2f3d;padding:16px 20px;font:17px/1.5 'DejaVu Sans Mono',monospace;white-space:pre-wrap;word-break:break-word;color:#ff7383}}
h2{{font-size:22px;margin:24px 0 8px}} .f{{color:#9aa0ad;font-size:18px;margin-top:18px}}</style>
<h1>Read-only test: write attempts denied</h1><div class=m>{e(head)}</div>
<div class=q>{e(req)}</div>{cards}
<h2>Key output lines (broker log and CLI)</h2><pre>{e(chr(10).join(keys))}</pre>
<div class=f>Local PoC. Rendered from evidence/negative-test.txt (pod IP removed). Source of truth: results.md Table 3.</div>"""
f = pathlib.Path(tempfile.mkdtemp()) / "d.html"
f.write_text(page)
subprocess.run([str(root / "scripts/shot.sh"), f"file://{f}", sys.argv[1], "1500"], check=True)

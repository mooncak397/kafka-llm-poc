#!/usr/bin/env python3
"""Build the 4 LinkedIn images (1200x627) in evidence/linkedin/. Values come from evidence/results.md Table 1.
Mean of two runs, rounded half up. Usage: make_linkedin.py"""
import re, subprocess, tempfile, pathlib
from decimal import Decimal as D, ROUND_HALF_UP
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "evidence/linkedin"
W, H = 1200, 627

# ---- numbers: read Table 1, average the two runs per level
rows = {}
for l in (root / "evidence/results.md").read_text().split("## Table 2")[0].splitlines():
    c = [x.strip() for x in l.strip("|").split("|")]
    if len(c) == 8 and c[0] in ("1", "10", "20"):
        rows.setdefault(c[0], []).append(c)
def avg(vals, places):
    m = sum(D(v) for v in vals) / len(vals)
    return str(m.quantize(D(1).scaleb(-places), ROUND_HALF_UP))
def pair(level, col, places):
    a = [[x.strip() for x in r[col].split("/")] for r in rows[level]]
    return avg([x[0] for x in a], places) + " / " + avg([x[1] for x in a], places)
stats = {}
for u in ("1", "10", "20"):
    ok = sum(int(r[2].split("/")[0]) for r in rows[u]); tot = sum(int(r[2].split("/")[1]) for r in rows[u])
    stats[u] = dict(ttft=pair(u, 4, 2), tps=avg([r[5] for r in rows[u]], 1), q=pair(u, 6, 2),
                    err=avg([r[3] for r in rows[u]], 2), req=f"{ok} / {tot}")
print(stats)

CSS = """*{box-sizing:border-box}body{margin:0;width:1200px;height:627px;background:#0f1117;color:#e3e6ec;font-family:'DejaVu Sans',sans-serif;position:relative;overflow:hidden}
.cap{position:absolute;left:0;right:0;bottom:0;height:44px;border-top:1px solid #2c3039;padding:0 40px;display:flex;align-items:center;color:#9aa0ad;font-size:17px}
.cap b{color:#f2cc0c;margin-right:10px}h1{margin:0;font-size:40px;line-height:1.15}.m{color:#9aa0ad}"""
def page(body, css=""): return f"<meta charset=utf-8><style>{CSS}{css}</style>{body}"

# ---- 1 title + simplified architecture
def box(x, y, w, h, t, s, col="#3a4150"):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#1c2028" stroke="{col}" stroke-width="2"/>'
            f'<text x="{x+16}" y="{y+32}" fill="#e3e6ec" font-size="21" font-weight="bold">{t}</text>'
            f'<text x="{x+16}" y="{y+58}" fill="#9aa0ad" font-size="16">{s}</text>')
def arrow(d, col, dash=""):
    return f'<path d="{d}" fill="none" stroke="{col}" stroke-width="3" {dash} marker-end="url(#a{col[1:]})"/>'
svg = f"""<svg width="1120" height="300" viewBox="0 0 1120 300" style="position:absolute;left:40px;top:170px" font-family="DejaVu Sans, sans-serif">
<defs>{''.join(f'<marker id="a{c}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#{c}"/></marker>' for c in ("5794f2","73bf69","ff9830"))}</defs>
<text x="0" y="16" fill="#9aa0ad" font-size="15" letter-spacing="1">ONE LAPTOP</text>
{box(0,34,250,80,"Agent loop","max 5 tool turns")}
{box(325,34,260,80,"MCP tools","5 read-only tools","#73bf69")}
{box(660,34,260,80,"Kafka 4.3.2","one broker, ACL denies writes","#73bf69")}
{box(0,170,250,80,"LLM metrics proxy","TTFT, tokens/s")}
{box(325,170,260,80,"Local LLM (Ollama)","qwen3:4b-instruct-2507")}
{box(660,170,300,80,"Prometheus + Grafana","dashboards","#ff9830")}
{box(950,34,170,80,"Fake data","producer, groups")}
{arrow("M250 74H325","#73bf69")}{arrow("M585 74H660","#73bf69")}
{arrow("M125 114V170","#5794f2")}{arrow("M250 210H325","#5794f2")}
{arrow("M950 74H920","#73bf69")}
{arrow("M125 250V278H810V250","#ff9830")}
{arrow("M810 114V170","#ff9830")}
</svg>"""
s1 = page(f"""<div style="padding:34px 40px 0"><h1>Kafka + local LLM with read-only tools</h1>
<div class=m style="font-size:21px;margin-top:12px;line-height:1.4">A local proof of concept: one laptop, one broker, fake data. No claim about production.</div></div>{svg}
<div class=cap><b>Local PoC</b> Simplified architecture. Blue: LLM requests. Green: Kafka and MCP. Orange: metrics.</div>""")

# ---- 2 load table
def col(u):
    d = stats[u]; n = "user" if u == "1" else "users"
    return f"""<div class=c><div class=u>{u} <span>{n}</span></div>
<div class=k>TTFT p50 / p95</div><div class=v>{d['ttft']} <i>s</i></div>
<div class=k>Tokens/s per user (median)</div><div class=v>{d['tps']}</div>
<div class=k>Error rate</div><div class=v>{d['err']} <i>({d['req']} ok)</i></div>
<div class=k>Queue wait p50 / p95 <em>derived</em></div><div class=v2>{d['q']} s</div></div>"""
s2 = page(f"""<div style="padding:30px 40px 0"><h1 style="font-size:36px">LLM load with 1, 10 and 20 users</h1>
<div class=m style="font-size:18px;margin-top:8px">Raw LLM mode, one slot, so extra users queue. Mean of two runs per level, 2026-11-15.</div></div>
<div class=g>{col('1')}{col('10')}{col('20')}</div>
<div class=cap><b>Local PoC</b> qwen3:4b-instruct-2507-q4_K_M, GTX 1060 6 GB, one laptop, fake data. Source: results.md Table 1.</div>""",
""".g{display:flex;gap:20px;padding:22px 40px 0}.c{flex:1;background:#161923;border:1px solid #2a2f3d;border-radius:10px;padding:16px 22px;height:404px}
.u{font-size:46px;font-weight:bold;color:#5794f2;line-height:1}.u span{font-size:20px;color:#9aa0ad;font-weight:normal}
.k{color:#9aa0ad;font-size:16px;margin-top:22px}.k em{color:#ff9830;font-style:normal;font-weight:bold;margin-left:6px}
.v{font-size:33px;font-weight:bold}.v i{font-size:16px;color:#9aa0ad;font-style:normal;font-weight:normal}.v2{font-size:28px;color:#c5c9d2}""")

# ---- 3 read-only denied
def card(t, a, b, lines):
    return f"<div class=c><div class=t>{t}</div><div class=a>{a}</div><div class=b>{b}</div></div>"
s3 = page(f"""<div style="padding:30px 40px 0"><h1 style="font-size:38px">Delete attempt denied</h1>
<div class=q>Request in all tests: "Delete the topic bikes.orders."</div></div>
<div class=g>
<div class=c><div class=t>(a) Tool filter on</div><div class=a>No delete tool, so no call</div><div class=b>3 of 3 runs. Nothing reached Kafka.</div></div>
<div class=c><div class=t>(b) Filter off on purpose</div><div class=a>Kafka denied 5 of 5 calls</div><div class=b>Model called delete-topics. Broker log: DefaultDeny for mcp-reader.</div></div>
<div class=c><div class=t>(c) CLI as mcp-reader</div><div class=a>TopicAuthorizationException</div><div class=b>Direct delete, no agent. Denied.</div></div></div>
<div class=ok>Topic still there after every test: <b>YES</b></div>
<div class=cap><b>Local PoC</b> Kafka 4.3.2, one broker, fake data, qwen3:4b-instruct-2507. Source: results.md Table 3.</div>""",
""".q{margin-top:12px;padding:10px 18px;background:#1a1d27;border-left:6px solid #5794f2;font-size:21px;color:#d8dbe2}
.g{display:flex;gap:20px;padding:20px 40px 0}.c{flex:1;background:#161923;border:1px solid #2a2f3d;border-radius:10px;padding:16px 20px;height:236px}
.t{color:#f2cc0c;font-weight:bold;font-size:19px}.a{font-size:25px;font-weight:bold;margin:12px 0 10px;line-height:1.2}.b{color:#9aa0ad;font-size:17px;line-height:1.4}
.ok{margin:34px 40px 0;font-size:34px;font-weight:bold}.ok b{color:#73bf69}""")

tmp = pathlib.Path(tempfile.mkdtemp())
def shoot(html, name):
    f = tmp / (name + ".html"); f.write_text(html)
    p = out / name
    subprocess.run(["google-chrome-stable", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        f"--user-data-dir={tmp}/prof", f"--window-size={W},{H}", "--virtual-time-budget=5000",
        f"--screenshot={p}", f"file://{f}"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p
files = [shoot(s1, "01-title.png"), shoot(s2, "02-load-1-vs-10-vs-20.png"), shoot(s3, "03-read-only-denied.png")]

# ---- 4 crop of 01-kafka-overview.png: status row + JVM and rate rows (empty "No data" panels left out), then caption
src = root / "evidence/01-kafka-overview.png"; p4 = out / "04-grafana-kafka.png"
cap = ("Local PoC: Strimzi Kafka dashboard, one broker, healthy. "
       "Red 'Partitions at min ISR' is expected. Empty panels cropped.")
subprocess.run(["magick", "(", str(src), "-crop", "1600x360+0+0", "+repage", ")", "(", str(src), "-crop", "1600x410+0+672", "+repage", ")",
    "-append", "-resize", "1200x577!", "(", "-size", "1120x", "-background", "#0f1117", "-fill", "#9aa0ad", "-font", "DejaVu-Sans",
    "-pointsize", "15", "-gravity", "West", f"caption:{cap}", "-bordercolor", "#0f1117", "-border", "40x6", ")", "-append",
    "-gravity", "North", "-background", "#0f1117", "-extent", f"{W}x{H}", str(p4)], check=True)
files.append(p4)
for p in files:
    subprocess.run(["magick", str(p), "-depth", "8", "-strip", str(p)], check=True)
    subprocess.run(["exiftool", "-all=", "-overwrite_original", "-q", str(p)], check=True)
    print(subprocess.check_output(["identify", "-format", "%f %wx%h %b\n", str(p)], text=True), end="")

#!/usr/bin/env python3
"""Build the standalone pack viewer: per-pack pages with layer toggles
(Core flow / + Evidence / Full), TD/LR switch, zoom and pan — fully offline
(vendored mermaid) — plus an index page listing every pack.

Usage: build-viewer.py <project-dir> <id>=<full.mmd> [<id>=<full.mmd> ...]
"""
import html
import json
import sys


def split_layers(full: str):
    """Derive the Core and +Evidence variants from the full diagram.

    The emitter's line grammar is deterministic (it is this repo's own
    runtime), so layers are classified per line: evidence subgraph blocks and
    reads/cites edges belong to the evidence layer; per-member "unknown"
    edges belong only to the full view.
    """
    core, evidence_only = [], []
    in_evidence = False
    for line in full.split("\n"):
        stripped = line.strip()
        if stripped.startswith("subgraph evidence["):
            in_evidence = True
        if in_evidence:
            evidence_only.append(line)
            if stripped == "end":
                in_evidence = False
            continue
        if '-. "reads" .->' in line or '-. "cites" .->' in line:
            evidence_only.append(line)
            continue
        if stripped == "applicability ~~~ evidence":
            evidence_only.append(line)
            core.append("  applicability ~~~ exceptions")
            continue
        if stripped == "evidence ~~~ exceptions":
            evidence_only.append(line)
            continue
        if '-. "unknown" .->' in line:
            continue
        if stripped.startswith('unresolved_unknown(['):
            # Its only edges are the unknown edges, which core and evidence
            # hide -- a declared node with no edge would float unexplained.
            continue
        core.append(line)
    core_text = "\n".join(core)
    evidence_text = core_text + "\n" + "\n".join(evidence_only)
    return core_text, evidence_text


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>__ID__ — pack diagram</title>
<script src="mermaid.min.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif;background:#fafafa;color:#1a1a1a}
  header{display:flex;align-items:center;gap:.6rem;padding:.5rem 1rem;border-bottom:1px solid #ddd;background:#fff;flex-wrap:wrap}
  header h1{font-size:1rem;margin:0 .5rem 0 0;font-weight:600}
  header a{color:#3355bb;text-decoration:none;font-size:.9rem}
  .seg{display:flex;border:1px solid #ccc;border-radius:6px;overflow:hidden}
  .seg button{border:0;border-right:1px solid #ccc;background:#fff;padding:.3rem .8rem;font:inherit;cursor:pointer}
  .seg button:last-child{border-right:0}
  .seg button.on{background:#e8edfb;font-weight:600}
  button.plain{font:inherit;padding:.3rem .8rem;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer}
  .hint{color:#777;font-size:.82rem;margin-left:auto}
  #viewport{position:relative;overflow:hidden;height:calc(100% - 53px);cursor:grab}
  #viewport.dragging{cursor:grabbing}
  #canvas{position:absolute;transform-origin:0 0}
</style>
</head>
<body>
<header>
  <a href="index.html">&larr; packs</a>
  <h1>__ID__</h1>
  <div class="seg" id="layers">
    <button data-l="core" class="on">Core flow</button>
    <button data-l="evidence">+ Evidence</button>
    <button data-l="full">Full</button>
  </div>
  <div class="seg" id="dir">
    <button data-d="TD" class="on">Top-down</button>
    <button data-d="LR">Left-right</button>
  </div>
  <button class="plain" id="fit">Fit</button>
  <span class="hint">scroll to zoom &middot; drag to pan &middot; double-click to fit &middot; deterministic render of the reviewed pack</span>
</header>
<div id="viewport"><div id="canvas"></div></div>
<script>
const SOURCES = __SOURCES__;
mermaid.initialize({startOnLoad:false,theme:"neutral",flowchart:{useMaxWidth:false},themeVariables:{fontSize:"15px"}});
const vp=document.getElementById("viewport"),cv=document.getElementById("canvas");
let s=1,tx=0,ty=0,layer="core",dir="TD",seq=0;
function apply(){cv.style.transform=`translate(${tx}px,${ty}px) scale(${s})`;}
function fit(){
  const svg=cv.querySelector("svg"); if(!svg) return;
  const w=svg.getBoundingClientRect().width/s, h=svg.getBoundingClientRect().height/s;
  s=Math.min(vp.clientWidth/w, vp.clientHeight/h)*0.95;
  tx=(vp.clientWidth-w*s)/2; ty=(vp.clientHeight-h*s)/2; apply();
}
async function render(){
  const src=SOURCES[layer].replace("flowchart TD","flowchart "+dir);
  const {svg}=await mermaid.render("d"+(seq++), src);
  cv.innerHTML=svg; fit();
}
document.getElementById("layers").addEventListener("click",e=>{
  if(!e.target.dataset.l) return;
  layer=e.target.dataset.l;
  for(const b of e.target.parentNode.children) b.classList.toggle("on",b===e.target);
  render();
});
document.getElementById("dir").addEventListener("click",e=>{
  if(!e.target.dataset.d) return;
  dir=e.target.dataset.d;
  for(const b of e.target.parentNode.children) b.classList.toggle("on",b===e.target);
  render();
});
vp.addEventListener("wheel",e=>{
  e.preventDefault();
  const k=e.deltaY<0?1.15:1/1.15, r=vp.getBoundingClientRect();
  const mx=e.clientX-r.left,my=e.clientY-r.top;
  tx=mx-(mx-tx)*k; ty=my-(my-ty)*k; s*=k; apply();
},{passive:false});
let drag=null;
vp.addEventListener("mousedown",e=>{drag={x:e.clientX-tx,y:e.clientY-ty};vp.classList.add("dragging");});
window.addEventListener("mousemove",e=>{if(drag){tx=e.clientX-drag.x;ty=e.clientY-drag.y;apply();}});
window.addEventListener("mouseup",()=>{drag=null;vp.classList.remove("dragging");});
vp.addEventListener("dblclick",fit);
document.getElementById("fit").onclick=fit;
render();
</script>
</body>
</html>
"""

INDEX = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Judgment packs — enterprise demo</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:44rem;margin:3rem auto;color:#1a1a1a;background:#fafafa;padding:0 1rem}
  h1{font-size:1.4rem} li{margin:.6rem 0;font-size:1.05rem}
  a{color:#3355bb} .q{color:#666;font-size:.92rem}
  footer{margin-top:2.5rem;color:#888;font-size:.85rem}
</style></head>
<body>
<h1>Judgment packs — enterprise demo</h1>
<p>Each diagram is a deterministic rendering of the reviewed pack document:
same bytes in, same picture out. It is a reading aid, never the pack.</p>
<ul>
__ITEMS__
</ul>
<footer>Rendered by <code>judgment-pack packs diagram</code>. The conformance claim is stated,
in full and only, in the runtime's CONFORMANCE.md.</footer>
</body>
</html>
"""


def main():
    project = sys.argv[1]
    items = []
    for spec in sys.argv[2:]:
        diagram_id, path = spec.split("=", 1)
        full = open(path).read()
        core, evidence = split_layers(full)
        sources = json.dumps({"core": core, "evidence": evidence, "full": full})
        page = PAGE.replace("__ID__", html.escape(diagram_id)).replace("__SOURCES__", sources)
        open(f"{project}/diagrams/{diagram_id}.html", "w").write(page)
        items.append(diagram_id)
    entries = "\n".join(
        f'<li><a href="{i}.html">{i}</a></li>' for i in items
    )
    open(f"{project}/diagrams/index.html", "w").write(INDEX.replace("__ITEMS__", entries))
    print("built:", ", ".join(items), "+ index")


main()

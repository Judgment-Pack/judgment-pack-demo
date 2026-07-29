#!/usr/bin/env python3
"""Build the self-contained zoomable diagram viewer for one pack diagram."""
import sys

HEAD = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>vendor-onboarding — pack diagram</title>
<script src="mermaid.min.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif;background:#fafafa;color:#1a1a1a}
  header{display:flex;align-items:center;gap:.75rem;padding:.5rem 1rem;border-bottom:1px solid #ddd;background:#fff}
  header h1{font-size:1rem;margin:0;font-weight:600}
  header .hint{color:#777;font-size:.85rem;margin-left:auto}
  button{font:inherit;padding:.25rem .7rem;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer}
  button:hover{background:#f0f0f0}
  #viewport{position:relative;overflow:hidden;height:calc(100% - 49px);cursor:grab}
  #viewport.dragging{cursor:grabbing}
  #canvas{position:absolute;transform-origin:0 0}
  #canvas svg{display:block}
</style>
</head>
<body>
<header>
  <h1>vendor-onboarding</h1>
  <button id="zin">+</button><button id="zout">&minus;</button>
  <button id="fit">Fit</button><button id="reset">100%</button>
  <span class="hint">scroll to zoom &middot; drag to pan &middot; double-click to fit &middot; rendered by <code>judgment-pack packs diagram</code></span>
</header>
<div id="viewport"><div id="canvas"><pre class="mermaid">
"""
TAIL = """</pre></div></div>
<script>
mermaid.initialize({startOnLoad:false,theme:"neutral",flowchart:{useMaxWidth:false},themeVariables:{fontSize:"15px"}});
const vp=document.getElementById("viewport"),cv=document.getElementById("canvas");
let s=1,tx=0,ty=0;
function apply(){cv.style.transform=`translate(${tx}px,${ty}px) scale(${s})`;}
function fit(){
  const svg=cv.querySelector("svg"); if(!svg) return;
  const w=svg.getBoundingClientRect().width/s, h=svg.getBoundingClientRect().height/s;
  s=Math.min(vp.clientWidth/w, vp.clientHeight/h)*0.95;
  tx=(vp.clientWidth-w*s)/2; ty=(vp.clientHeight-h*s)/2; apply();
}
mermaid.run({querySelector:".mermaid"}).then(fit);
vp.addEventListener("wheel",e=>{
  e.preventDefault();
  const k=e.deltaY<0?1.15:1/1.15, r=vp.getBoundingClientRect();
  const mx=e.clientX-r.left, my=e.clientY-r.top;
  tx=mx-(mx-tx)*k; ty=my-(my-ty)*k; s*=k; apply();
},{passive:false});
let drag=null;
vp.addEventListener("mousedown",e=>{drag={x:e.clientX-tx,y:e.clientY-ty};vp.classList.add("dragging");});
window.addEventListener("mousemove",e=>{if(drag){tx=e.clientX-drag.x;ty=e.clientY-drag.y;apply();}});
window.addEventListener("mouseup",()=>{drag=null;vp.classList.remove("dragging");});
vp.addEventListener("dblclick",fit);
document.getElementById("zin").onclick=()=>{s*=1.25;apply();};
document.getElementById("zout").onclick=()=>{s/=1.25;apply();};
document.getElementById("fit").onclick=fit;
document.getElementById("reset").onclick=()=>{s=1;tx=20;ty=20;apply();};
</script>
</body>
</html>
"""

diagram_id, mmd_path = sys.argv[1], sys.argv[2]
head = HEAD.replace("vendor-onboarding", diagram_id)
sys.stdout.write(head + open(mmd_path).read() + TAIL)

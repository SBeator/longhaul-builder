#!/usr/bin/env python3
"""gantt.py —— 把一次运行的 events.jsonl 渲成一张「交互甘特」自包含 HTML（零三方依赖）。

每条=一个 milestone；横向色块=该步在某时刻干了多久（出方案/写实现/判官审/超时/返工）；
hover 方块看详情、hover 左侧名称看该步失败时间线、右上角横向缩放。内容全来自真实 step_timing +
失败事件（门1/2 裁定·超时·熔断·举旗·驳回），不靠 agent 自述。

被 reportdoc.py「耗时」段嵌入（md/html）；飞书走妙笔 HTML Box（bindings）。stdlib only、可移植。
"""
import argparse
import datetime
import json
import os
import sys

SUBSTANTIVE = ("driver", "review", "verify")
_FAIL_LBL = {"reopen_plan": "门1 REVISE→重规划", "fail": "判失败(返工)", "circuit_break": "熔断",
             "self_recovery": "自救·换approach", "flag_raised": "举旗", "flag_rejected": "驳回·重做",
             "redirect_note": "人工 redirect", "milestone_split": "拆分为子步"}


def _load_events(state_dir):
    p = os.path.join(state_dir, "events.jsonl")
    out = []
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _load_milestones(state_dir):
    p = os.path.join(state_dir, "milestones.json")
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d.get("milestones", d) if isinstance(d, dict) else d
    except ValueError:
        return []


def _utc(s):
    return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def extract(state_dir):
    """从 events.jsonl + milestones.json 抽出甘特数据（rows/bars/marks/meta）。"""
    evs = _load_events(state_dir)
    ms = _load_milestones(state_dir)
    if not evs or not ms:
        return None
    run_start, run_end = _utc(evs[0]["ts"]), _utc(evs[-1]["ts"])
    span = max(1, int((run_end - run_start).total_seconds()))
    start_local = run_start + datetime.timedelta(hours=8)

    def off(ts):
        return int((_utc(ts) - run_start).total_seconds())

    # 超时事件（精确标"白跑"）
    to_ev = [(e["milestone"], _utc(e["ts"]), e.get("reason", "")) for e in evs
             if e.get("ev") == "infra_retry" and "timed out" in (e.get("reason") or "")]

    def to_reason(mid, end_s):
        end = run_start + datetime.timedelta(seconds=end_s)
        for m, t, r in to_ev:
            if m == mid and abs((end - t).total_seconds()) <= 90:
                return r
        return None

    ids = {m["id"] for m in ms}
    bars = []
    for e in evs:
        if e.get("ev") != "step_timing":
            continue
        step = e.get("step", "")
        if step not in SUBSTANTIVE or step == "verify":
            continue
        mid = e.get("milestone")
        if mid not in ids:
            continue
        st = e.get("started")
        dur = (e.get("duration_ms") or 0) / 1000.0
        if not st or dur < 1:
            continue
        if step == "driver":
            cat = e.get("phase", "impl")
            if to_reason(mid, off(st) + int(dur)):
                cat = "timeout"
        else:
            cat = "review"
        b = {"mid": mid, "off": off(st), "dur": int(dur), "cat": cat}
        if cat == "timeout":
            b["reason"] = to_reason(mid, off(st) + int(dur))
        bars.append(b)

    def clip(s, n=240):
        return (s or "").replace("\n", " ").strip()[:n]

    ev_rows = {m["id"]: [] for m in ms}
    marks = []
    for e in evs:
        mid = e.get("milestone")
        if mid not in ids:
            continue
        t = e.get("ev")
        if t not in _FAIL_LBL:
            continue
        if t == "infra_retry":
            continue
        if t == "reopen_plan":
            reason = clip(e.get("error", "门1 REVISE"))
        elif t == "fail":
            reason = "%s（attempt %s）" % (clip(e.get("error")), e.get("attempt", ""))
        elif t == "circuit_break":
            reason = "%s（连失败到 attempt %s 上限）" % (clip(e.get("last_error")), e.get("attempt_count", ""))
        elif t == "flag_raised":
            reason = "%s：%s" % (e.get("kind", ""), clip(e.get("summary")))
        elif t == "flag_rejected":
            reason = clip(e.get("instruction"))
        elif t == "milestone_split":
            reason = "拆成：%s" % ", ".join(e.get("into") or [])
        elif t == "self_recovery":
            reason = "撞上限先自救、换 approach 重试一次"
        else:
            reason = ""
        lbl = _FAIL_LBL[t]
        ev_rows.setdefault(mid, []).append({"off": off(e["ts"]), "label": lbl, "reason": reason})
        marks.append({"mid": mid, "off": off(e["ts"]), "label": lbl, "reason": reason})

    # crash 类 infra_retry（非超时）单独进 marks
    for e in evs:
        if e.get("ev") == "infra_retry" and "timed out" not in (e.get("reason") or "") and e.get("milestone") in ids:
            mid = e["milestone"]
            r = clip(e.get("reason"))
            ev_rows.setdefault(mid, []).append({"off": off(e["ts"]), "label": "driver 异常重试", "reason": r})
            marks.append({"mid": mid, "off": off(e["ts"]), "label": "driver 异常重试", "reason": r})

    rows = []
    for m in ms:
        mid = m["id"]
        bs = [b for b in bars if b["mid"] == mid]
        rows.append({
            "mid": mid, "goal": m.get("goal", ""), "status": m.get("status", ""),
            "drv_min": round(sum(b["dur"] for b in bs if b["cat"] in ("plan", "impl", "timeout")) / 60, 1),
            "rev_min": round(sum(b["dur"] for b in bs if b["cat"] == "review") / 60, 1),
            "n_drv": sum(1 for b in bs if b["cat"] in ("plan", "impl", "timeout")),
            "events": sorted(ev_rows.get(mid, []), key=lambda x: x["off"]),
        })
    return {
        "start_local": start_local.strftime("%Y-%m-%d %H:%M"),
        "start_epoch_local": int((start_local - datetime.datetime(1970, 1, 1)).total_seconds()),
        "end_local": (run_end + datetime.timedelta(hours=8)).strftime("%H:%M"),
        "span": span, "hours": round(span / 3600, 1), "rows": rows, "bars": bars, "marks": marks,
    }


def build_html(state_dir, title="运行流水（交互甘特）", standalone=True):
    """渲交互甘特 HTML。standalone=True 出完整页面；False 出可嵌入片段（飞书 HTML Box 用）。"""
    data = extract(state_dir)
    if not data:
        return "<p>（无 step_timing 数据——可能还没跑或旧版引擎）</p>"
    body = _GANTT_FRAGMENT.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace("__TITLE__", title)
    if not standalone:
        return body
    return _PAGE_SHELL.replace("__TITLE__", title).replace("__BODY__", body)


_PAGE_SHELL = """<!DOCTYPE html><html lang="zh-CN" data-theme="dark"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="use-iframe" content="true"><meta name="html-box-height-mode" content="auto">
<title>__TITLE__</title></head><body style="margin:0">__BODY__</body></html>"""


_GANTT_FRAGMENT = r"""
<style>
.lhg{--bg:#111217;--card:rgba(255,255,255,.04);--card2:rgba(255,255,255,.07);--tp:#f7f8fb;--ts:#b5bac4;
  --tm:#858b96;--bd:rgba(255,255,255,.12);--bdc:rgba(255,255,255,.22);--cyan:#3ec3f7;--soft:#80a3ff;
  --g-plan:#3ec3f7;--g-impl:#ED7D31;--g-review:#5BBF73;--g-to:#C0392B;--g-rw:#A98BE0;
  --rowh:30px;--axh:22px;--lblw:200px;
  background:var(--bg);color:var(--tp);font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;padding:18px;box-sizing:border-box}
.lhg *{box-sizing:border-box}
.lhg h3{font-size:18px;font-weight:600;margin:0 0 6px}
.lhg .sub{font-size:13px;color:var(--ts);margin-bottom:10px;line-height:1.5}
.lhg .bar2,.lhg .tb{display:flex;align-items:center;flex-wrap:wrap;gap:10px 16px;margin-bottom:12px}
.lhg .zb{width:30px;height:28px;border:1px solid var(--bdc);background:var(--card2);color:var(--tp);
  border-radius:6px;font-size:15px;cursor:pointer;line-height:1}.lhg .zb.fit{width:auto;padding:0 12px;font-size:12px}
.lhg .zb:hover{background:rgba(20,86,240,.18)}
.lhg .zv{font-size:12px;color:var(--tm);font-family:monospace}
.lhg .lg{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--ts)}
.lhg .sw{width:18px;height:11px;border-radius:4px;display:inline-block}
.lhg .sw.to{background:repeating-linear-gradient(45deg,var(--g-to),var(--g-to) 3px,#7d2018 3px,#7d2018 6px)}
.lhg .chart{background:var(--card);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.lhg .gantt{display:flex}
.lhg .labels{flex:0 0 var(--lblw);width:var(--lblw);border-right:1px solid var(--bdc);background:var(--bg);z-index:2}
.lhg .scroll{flex:1;overflow-x:auto;overflow-y:hidden}
.lhg .axsp{height:var(--axh);border-bottom:1px solid var(--bd)}
.lhg .lblr{height:var(--rowh);display:flex;align-items:center;padding:0 8px;border-bottom:1px solid rgba(255,255,255,.05);
  font-size:11.5px;color:var(--ts);white-space:nowrap;overflow:hidden;cursor:default}
.lhg .lblr:hover{background:var(--card2)}
.lhg .lblr b{color:var(--tp);font-weight:600;margin-right:5px;font-family:monospace}
.lhg .lblr.bad b{color:#ff9b8a}.lhg .lblr .st{color:var(--soft);margin-left:4px;font-size:10px}
.lhg .lblr .gt{overflow:hidden;text-overflow:ellipsis}
.lhg .canvas{position:relative}
.lhg .axis{position:relative;height:var(--axh);border-bottom:1px solid var(--bd)}
.lhg .tick{position:absolute;top:0;height:100%;border-left:1px solid var(--bd);padding-left:3px;
  font-size:10px;color:var(--tm);font-family:monospace}
.lhg .trow{position:relative;height:var(--rowh);border-bottom:1px solid rgba(255,255,255,.05)}
.lhg .grid{position:absolute;top:0;bottom:0;border-left:1px solid rgba(255,255,255,.045)}
.lhg .bar{position:absolute;top:6px;height:18px;border-radius:4px;cursor:pointer;min-width:2px;
  border:1px solid rgba(0,0,0,.28);transition:filter .1s}
.lhg .bar:hover{filter:brightness(1.4);z-index:5}
.lhg .bar.plan{background:var(--g-plan)}.lhg .bar.impl{background:var(--g-impl)}.lhg .bar.review{background:var(--g-review)}
.lhg .bar.timeout{background:repeating-linear-gradient(45deg,var(--g-to),var(--g-to) 4px,#7d2018 4px,#7d2018 8px);border-color:#7d2018}
.lhg .mark{position:absolute;top:1px;width:0;height:0;cursor:pointer;border-left:5px solid transparent;
  border-right:5px solid transparent;border-top:8px solid var(--g-rw);transform:translateX(-5px);z-index:6}
.lhg .foot{font-size:11px;color:var(--tm);margin-top:10px;line-height:1.5}
.lhg-tip{position:fixed;z-index:99;max-width:360px;background:rgba(30,33,45,.97);border:1px solid var(--bdc,#7a86a8);
  border-radius:6px;padding:10px;font-size:12px;line-height:1.5;color:#f7f8fb;display:none;pointer-events:none;
  box-shadow:0 6px 24px rgba(0,0,0,.5)}
.lhg-tip .t{font-weight:600;margin-bottom:3px}.lhg-tip .g{color:#b5bac4;margin-bottom:6px}
.lhg-tip .kv b{color:#f7f8fb}.lhg-tip .warn{color:#ff9b8a;margin-top:5px}
.lhg-tip .ev{margin-top:6px;border-top:1px solid rgba(255,255,255,.15);padding-top:5px}
.lhg-tip .ei{margin-top:5px}.lhg-tip .et{color:#A98BE0;font-weight:600}.lhg-tip .et.to{color:#ff9b8a}
.lhg-tip .tm{font-family:monospace;color:#858b96}
</style>
<div class="lhg" id="lhg">
  <h3>__TITLE__</h3>
  <div class="sub" id="lhg-sub"></div>
  <div class="bar2">
    <span class="tb"><span style="font-size:12px;color:var(--ts)">横向缩放</span>
      <button class="zb" id="lhg-out">−</button><button class="zb fit" id="lhg-fit">适应</button>
      <button class="zb" id="lhg-in">＋</button><span class="zv" id="lhg-zv">1.0×</span></span>
    <span class="lg"><i class="sw" style="background:var(--g-plan)"></i>出方案</span>
    <span class="lg"><i class="sw" style="background:var(--g-impl)"></i>写实现</span>
    <span class="lg"><i class="sw" style="background:var(--g-review)"></i>判官审</span>
    <span class="lg"><i class="sw to"></i>超时白跑</span>
    <span class="lg"><i class="sw" style="width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-top:8px solid var(--g-rw)"></i>返工/异常</span>
  </div>
  <div class="chart"><div class="gantt">
    <div class="labels" id="lhg-labels"><div class="axsp"></div></div>
    <div class="scroll" id="lhg-scroll"><div class="canvas" id="lhg-canvas"><div class="axis" id="lhg-axis"></div></div></div>
  </div></div>
  <div class="foot" id="lhg-foot"></div>
</div>
<div class="lhg-tip" id="lhg-tip"></div>
<script>(function(){
var DATA=__DATA__;
var CAT={plan:"出方案 plan",impl:"写实现 impl",review:"判官审 review",timeout:"写实现·撞超时白跑"};
var span=DATA.span,startE=DATA.start_epoch_local;
var $=function(id){return document.getElementById(id)};
function p2(n){return(""+n).padStart(2,"0")}
function hhmm(o){var d=new Date((startE+o)*1000);return p2(d.getUTCHours())+":"+p2(d.getUTCMinutes())}
function hms(o){var d=new Date((startE+o)*1000);return hhmm(o)+":"+p2(d.getUTCSeconds())}
function dur(s){if(s<60)return s+"s";var m=Math.floor(s/60),r=s%60;return r?m+"分"+r+"秒":m+"分"}
function esc(s){return(s||"").replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]})}
function sg(g){g=(g||"").split("：")[0];return g.length>18?g.slice(0,18):g}
var lblw=(window.innerWidth<=560)?96:200;$("lhg").style.setProperty("--lblw",lblw+"px");
var last=DATA.rows.length?DATA.rows[DATA.rows.length-1].mid:"";
for(var i=DATA.rows.length-1;i>=0;i--){if(DATA.rows[i].n_drv>0||DATA.rows[i].status!=="TODO"){last=DATA.rows[i].mid;break}}
$("lhg-sub").textContent="从 "+DATA.start_local+" → "+DATA.end_local+"（本地），约 "+DATA.hours+
  " 小时、"+DATA.rows.length+" 个 milestone、"+DATA.bars.length+" 段执行。悬停方块/▽看详情，悬停左侧名称看该步失败时间线，右上可横向放大。";
var gh=[];for(var t=Math.ceil(startE/3600)*3600;t<startE+span;t+=3600)gh.push(t-startE);
var labels=$("lhg-labels"),canvas=$("lhg-canvas"),axis=$("lhg-axis"),byMid={};
DATA.rows.forEach(function(r){
  var bad=r.events.length>0;var d=document.createElement("div");d.className="lblr"+(bad?" bad":"");
  d.innerHTML="<b>"+r.mid+"</b><span class='gt'>"+esc(sg(r.goal))+"</span>"+(r.status!=="DONE"?"<span class='st'>["+r.status+"]</span>":"");
  bindTip(d,function(){return rowTip(r)});labels.appendChild(d);
  var tr=document.createElement("div");tr.className="trow";canvas.appendChild(tr);r._tr=tr;byMid[r.mid]=r;
});
var PPS=0,fitPPS=0,zoom=1;
function render(){
  PPS=fitPPS*zoom;var W=Math.round(span*PPS);canvas.style.width=W+"px";axis.innerHTML="";
  gh.forEach(function(o){var x=Math.round(o*PPS);var tk=document.createElement("div");tk.className="tick";
    tk.style.left=x+"px";tk.textContent=p2(new Date((startE+o)*1000).getUTCHours())+":00";axis.appendChild(tk)});
  DATA.rows.forEach(function(r){r._tr.innerHTML="";gh.forEach(function(o){var g=document.createElement("div");
    g.className="grid";g.style.left=Math.round(o*PPS)+"px";r._tr.appendChild(g)})});
  DATA.bars.forEach(function(b){var r=byMid[b.mid];if(!r)return;var el=document.createElement("div");
    el.className="bar "+b.cat;el.style.left=Math.round(b.off*PPS)+"px";el.style.width=Math.max(Math.round(b.dur*PPS),2)+"px";
    bindTip(el,function(){return barTip(b,r)});r._tr.appendChild(el)});
  DATA.marks.forEach(function(m){var r=byMid[m.mid];if(!r)return;var el=document.createElement("div");
    el.className="mark";el.style.left=Math.round(m.off*PPS)+"px";bindTip(el,function(){return markTip(m)});r._tr.appendChild(el)});
  $("lhg-zv").textContent=zoom.toFixed(1)+"×";fit();
}
function rowTip(r){var bs=DATA.bars.filter(function(b){return b.mid===r.mid});var wall="";
  if(bs.length){var s=Math.min.apply(0,bs.map(function(b){return b.off})),e=Math.max.apply(0,bs.map(function(b){return b.off+b.dur}));wall=hhmm(s)+"–"+hhmm(e)+"（跨 "+dur(e-s)+"）"}
  var h="<div class='t'"+(r.events.length?" style='color:#ff9b8a'":"")+">"+r.mid+"　"+esc((r.goal||"").split("：")[0])+(r.status!=="DONE"?" ["+r.status+"]":"")+"</div>";
  h+="<div class='g'>"+esc(r.goal)+"</div><div class='kv'>driver <b>"+r.drv_min+"分</b>（"+r.n_drv+"次）· review <b>"+r.rev_min+"分</b>"+(wall?" · 墙钟 "+wall:"")+"</div>";
  if(r.events.length){h+="<div class='ev'><b style='color:#ff9b8a'>⚠ 失败/返工时间线（"+r.events.length+"）</b>";
    r.events.forEach(function(e){var to=e.label.indexOf("超时")>=0;h+="<div class='ei'><span class='tm'>"+hms(e.off)+"</span> <span class='et"+(to?" to":"")+"'>"+esc(e.label)+"</span><br><span class='g'>"+esc(e.reason)+"</span></div>"})
    h+="</div>"}else{h+="<div class='ev' style='color:#7fd99a'>✓ 顺利：出方案→写实现→判官审，无返工。</div>"}return h}
function barTip(b,r){var h="<div class='t'>"+b.mid+"　"+(CAT[b.cat]||b.cat)+"</div><div class='g'>"+esc(r.goal)+"</div>";
  h+="<div class='kv'>时间 <b>"+hms(b.off)+" – "+hms(b.off+b.dur)+"</b> · 本步 <b>"+dur(b.dur)+"</b></div>";
  if(b.cat==="timeout")h+="<div class='warn'>⚠ 撞超时被丢弃重来（白跑）："+esc(b.reason||"")+"</div>";return h}
function markTip(m){return"<div class='t' style='color:"+(m.label.indexOf("超时")>=0?"#ff9b8a":"#A98BE0")+"'>"+m.mid+" · "+esc(m.label)+"</div><div class='kv tm'>"+hms(m.off)+"</div><div class='g' style='margin-top:4px'>"+esc(m.reason)+"</div>"}
var tip=$("lhg-tip"),pinned=false;
function bindTip(el,fn){el.addEventListener("pointerenter",function(e){show(e,fn())});el.addEventListener("pointermove",mv);
  el.addEventListener("pointerleave",hide);el.addEventListener("click",function(e){e.stopPropagation();pin(e,fn())})}
function show(e,h){if(pinned)return;tip.innerHTML=h;tip.style.display="block";place(e)}
function mv(e){if(!pinned)place(e)}
function place(e){var pad=14,x=e.clientX+pad,y=e.clientY+pad,w=tip.offsetWidth,hh=tip.offsetHeight;
  if(x+w>innerWidth-8)x=e.clientX-w-pad;if(y+hh>innerHeight-8)y=e.clientY-hh-pad;tip.style.left=Math.max(8,x)+"px";tip.style.top=Math.max(8,y)+"px"}
function hide(){if(!pinned)tip.style.display="none"}
function pin(e,h){pinned=false;tip.innerHTML=h;tip.style.display="block";place(e);pinned=true}
document.addEventListener("click",function(){pinned=false;tip.style.display="none"});
function setZoom(z,ar){var sc=$("lhg-scroll"),old=zoom;zoom=Math.min(14,Math.max(1,z));
  var ratio=(ar!=null)?ar:((sc.scrollLeft+sc.clientWidth/2)/Math.max(1,span*fitPPS*old));render();sc.scrollLeft=Math.max(0,ratio*span*PPS-sc.clientWidth/2)}
$("lhg-in").onclick=function(){setZoom(zoom*1.5)};$("lhg-out").onclick=function(){setZoom(zoom/1.5)};
$("lhg-fit").onclick=function(){zoom=1;render();$("lhg-scroll").scrollLeft=0};
$("lhg-scroll").addEventListener("wheel",function(e){if(e.ctrlKey||e.metaKey){e.preventDefault();
  var sc=$("lhg-scroll"),ax=(sc.scrollLeft+e.offsetX)/Math.max(1,span*PPS);setZoom(zoom*(e.deltaY<0?1.2:1/1.2),ax)}},{passive:false});
function cf(){var sc=$("lhg-scroll");fitPPS=Math.max(.0001,(sc.clientWidth-2)/span)}
function fit(){if(window.magic&&window.magic.updateHeight){try{window.magic.updateHeight()}catch(e){}}}
$("lhg-foot").innerHTML="数据：.longhaul/events.jsonl 的 step_timing + 失败事件（门1/2 裁定·超时·熔断·举旗·驳回），真实非估算。";
function boot(){cf();render()}window.addEventListener("load",boot);
window.addEventListener("resize",function(){var z=zoom;cf();zoom=z;render()});setTimeout(boot,250);
})();</script>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="交互甘特 HTML（从证据渲染）")
    ap.add_argument("state_dir")
    ap.add_argument("--title", default="运行流水（交互甘特）")
    ap.add_argument("--fragment", action="store_true", help="出可嵌入片段（飞书 HTML Box 用）")
    a = ap.parse_args(argv)
    sys.stdout.write(build_html(a.state_dir, a.title, standalone=not a.fragment))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

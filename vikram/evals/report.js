/* Self-contained benchmark views. Model data is rendered only as text. */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const runs = JSON.parse(byId("run-data").textContent);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const pct = (value) => value == null ? "Unavailable" : `${(100 * value).toFixed(1)}%`;
  const delta = (a, b) => a == null || b == null ? "—" : `${b > a ? "+" : ""}${(100 * (b - a)).toFixed(1)} pp`;
  const score = (run, k, group = "combined") => run?.scores[group]?.[k] ?? null;
  const short = (value) => value ? value.slice(0, 12) : "Not recorded";
  const date = (run) => new Date(run.started_at).toLocaleString();
  const name = (run) => run.label || run.config.model;
  const thinking = (value) => value == null ? "Default / unspecified" : value === true ? "On" : value === false ? "Off" : value;
  const levelKey = (value) => JSON.stringify(value ?? null);
  const title = (run) => `${date(run)} · ${run.config.model} · ${thinking(run.config.thinking)}${run.label ? ` · ${run.label}` : ""} · ${run.run_id.slice(-8)}`;
  const versions = (run) => run.revision ? Object.entries(run.revision.harness)
    .sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key} ${value}`).join("; ") : null;
  const settings = [
    ["Model", (r) => r.config.model],
    ["Thinking", (r) => r.config.thinking == null ? null : thinking(r.config.thinking)],
    ["System prompt", (r) => r.agent_hash, short],
    ["Runtime / tools code", (r) => r.revision?.source_hash, short],
    ["Dependencies", (r) => r.revision?.dependencies_hash, short],
    ["Harness versions", versions],
    ["Temperature", (r) => r.config.temperature],
    ["Answer timeout (seconds)", (r) => r.config.timeout_seconds],
    ["Samples per case", (r) => r.config.samples],
  ];
  const differences = (a, b) => settings.filter(([, value]) =>
    value(a) != null && value(b) != null && value(a) !== value(b)).map(([label]) => label);
  const groups = [["combined", "Combined", "#172a32"], ["objective", "Objective checks", "#27658d"], ["judged", "Rubric judge", "#956112"]];
  const metricColors = [[1, "#376d9e"], [3, "#c47a22"]];
  const mean = (values) => values.length ? values.reduce((a,b) => a+b, 0) / values.length : null;
  const countOf = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;
  const seconds = (value) => value == null ? "Unavailable" : `${value.toFixed(2)}s`;
  const eligible = (entry, k) => entry.runs.filter((run) => score(run,k) != null);
  const average = (entry, k) => mean(eligible(entry,k).map((run) => score(run,k)));
  const wallTime = (entry) => mean(eligible(entry,1).filter((run) => run.duration_seconds > 0)
    .map((run) => run.duration_seconds / (run.cases.length * run.config.samples)));
  const entryTitle = (entry) => `${entry.run.config.model} · thinking ${thinking(entry.level)}${entry.revisionLabel ? ` · ${entry.revisionLabel}` : ""}`;

  function cell(row, text, metric = false) {
    const td = node("td", text);
    if (metric) td.dataset.metric = "true";
    row.append(td);
    return td;
  }
  function header(table, labels) {
    const row = node("tr");
    row.append(...labels.map((label) => node("th", label)));
    table.querySelector("thead").replaceChildren(row);
  }
  function change(a, b) {
    const item = node("div", delta(a, b), "caption");
    if (a != null && b != null && a !== b) item.dataset.direction = b > a ? "up" : "down";
    return item;
  }
  function status(value) {
    const badge = node("span", value);
    badge.dataset.status = value;
    return badge;
  }
  function svgNode(tag, attributes = {}, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function chart(target, items, k, inspect) {
    const left = 40, right = 365, top = 20, bottom = 190;
    const svg = svgNode("svg", {viewBox:"0 0 405 235", role:"group", "aria-label":`pass@${k} over time`});
    svg.append(svgNode("title", {}, `pass@${k} across all agent configurations`));
    const times = items.map((run) => new Date(run.started_at).getTime());
    const min = times[0], max = times.at(-1);
    const x = (i) => max === min ? (left + right) / 2 : left + (times[i] - min) / (max - min) * (right - left);
    const y = (value) => bottom - value * (bottom - top);
    for (const value of [0, 0.25, 0.5, 0.75, 1]) {
      svg.append(svgNode("line", {x1:left, x2:right, y1:y(value), y2:y(value), stroke:"#e1e6e1"}));
      svg.append(svgNode("text", {x:left-7, y:y(value)+4, "text-anchor":"end"}, `${value*100}%`));
    }
    for (const index of new Set([0, Math.floor((items.length-1)/2), items.length-1])) {
      const instant = new Date(items[index].started_at);
      const label = max - min < 86400000
        ? instant.toLocaleTimeString(undefined, {hour:"2-digit", minute:"2-digit", ...(max-min < 3600000 ? {second:"2-digit"} : {})})
        : instant.toLocaleDateString(undefined, {month:"short", day:"numeric"});
      svg.append(svgNode("text", {x:x(index), y:bottom+25, "text-anchor":"middle"}, label));
    }
    for (const [group, label, color] of groups) {
      let segment = [];
      const flush = () => {
        if (segment.length > 1) svg.append(svgNode("polyline", {points:segment.join(" "), fill:"none", stroke:color, "stroke-width":group === "combined" ? 2.5 : 1.5, "stroke-dasharray":group === "combined" ? "none" : "4 3"}));
        segment = [];
      };
      items.forEach((run, i) => {
        const value = score(run, k, group);
        if (value == null) { flush(); return; }
        segment.push(`${x(i)},${y(value)}`);
      });
      flush();
      items.forEach((run, i) => {
        const value = score(run, k, group);
        if (value == null) return;
        const tooltip = `${title(run)} · ${run.config.model} · ${label}: ${pct(value)}`;
        const circle = svgNode("circle", {cx:x(i), cy:y(value), r:group === "combined" ? 4 : 2.7, fill:color, stroke:"white", "stroke-width":0.7, tabindex:"0", role:"button", "aria-label":`Inspect ${tooltip}`});
        circle.append(svgNode("title", {}, tooltip));
        circle.addEventListener("click", () => inspect(run));
        circle.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); inspect(run); }
        });
        svg.append(circle);
      });
    }
    items.forEach((run, i) => {
      if (score(run, k) != null) return;
      const mark = svgNode("text", {x:x(i), y:bottom-5, "text-anchor":"middle"}, "×");
      mark.append(svgNode("title", {}, `${title(run)} · combined score unavailable`));
      svg.append(mark);
    });
    target.append(svg);
  }

  function pairedBars(target, entries, inspect) {
    const width = window.innerWidth < 640 ? 350 : 440;
    const left = 10, right = width-60;
    const axis = node("div");
    axis.dataset.role="bar-axis";
    const ticks = svgNode("svg",{viewBox:`0 0 ${width} 22`,role:"img","aria-label":"Success probability from 0 to 100 percent"});
    for (const value of [0,.25,.5,.75,1]) ticks.append(svgNode("text",{x:left+(right-left)*value,y:15,"text-anchor":value===0 ? "start" : value===1 ? "end" : "middle"},`${value*100}%`));
    axis.append(node("span"),ticks); target.append(axis);
    for (const entry of entries) {
      const row = node("div"), label = node("div");
      row.dataset.thinking = thinking(entry.level);
      const heading = node(entry.runs.length ? "button" : "strong", entry.run.config.model);
      if (entry.runs.length) heading.addEventListener("click", () => inspect(entry.runs.at(-1)));
      const values = metricColors.map(([k]) => `pass@${k}: ${entry.runs.length ? pct(average(entry,k)) : "Not run"}`);
      label.append(heading, node("div", `Thinking ${thinking(entry.level)}${entry.revisionLabel ? ` · ${entry.revisionLabel}` : ""}`, "caption"));
      const svg = svgNode("svg", {viewBox:`0 0 ${width} 42`, role:"img", "aria-label":`${entryTitle(entry)} · ${values.join(" · ")}`});
      for (const value of [0,.25,.5,.75,1]) svg.append(svgNode("line",{x1:left+(right-left)*value,x2:left+(right-left)*value,y1:0,y2:40,stroke:"#e8ecf1","stroke-width":.75}));
      for (const [index,[k,color]] of metricColors.entries()) {
        const y = 5 + index*20, value = average(entry,k);
        svg.append(svgNode("rect", {x:left,y,width:right-left,height:10,rx:1,fill:"#f1f3f6"}));
        if (value != null) svg.append(svgNode("rect", {x:left,y,width:(right-left)*value,height:10,rx:1,fill:color,"data-k":k}));
        svg.append(svgNode("text", {x:right+8,y:y+9}, value == null ? "—" : pct(value)));
      }
      if (!entry.runs.length || metricColors.some(([k]) => average(entry,k)==null)) label.append(node("div",values.join(" · "),"caption"));
      row.append(label,svg); target.append(row);
    }
  }

  function findings(part, entries, items) {
    const verdict = (label, candidates, value, format, minimize = false) => {
      const measured = candidates.filter((entry) => value(entry) != null);
      const best = measured.length ? (minimize ? Math.min : Math.max)(...measured.map(value)) : null;
      const winners = measured.filter((entry) => Math.abs(value(entry)-best) < 1e-9);
      const card = node("section");
      card.append(node("div",label,"eyebrow"), node("strong", best == null ? "Not available" : format(best)),
        node("p", winners.length === 1 ? entryTitle(winners[0]) : winners.length ? `${winners.length} configurations tied` : "Needs complete, eligible runs.","caption"));
      if (winners.length>1) {
        const details=node("details"), list=node("ul");
        list.append(...winners.map((entry)=>node("li",entryTitle(entry))));
        details.append(node("summary","View tied models"),list); card.append(details);
      }
      part("verdicts").append(card);
    };
    verdict("Best observed pass@1", entries, (entry) => average(entry,1), pct);
    verdict("Best observed pass@3", entries, (entry) => average(entry,3), pct);
    verdict("Lowest eval time / attempt", entries, wallTime, seconds, true);
    const notes = [];
    const complete = items.filter((run) => score(run,1) != null);
    notes.push(`${complete.length} of ${countOf(items.length,"run")} complete across ${countOf(entries.filter((entry) => entry.runs.length).length,"measured configuration")}. ${countOf(items.at(-1).cases.length,"case")} per run; each case has equal weight.`);
    if (complete.length > 1) {
      const first = complete[0], last = complete.at(-1);
      const changed = differences(first,last);
      notes.push(`Latest versus first complete run: ${delta(score(first,1),score(last,1))} at pass@1 and ${delta(score(first,3),score(last,3))} at pass@3. Recorded changes: ${changed.join(", ") || "none"}.`);
    } else notes.push("More complete runs are needed to establish a development trend.");
    const gains = entries.map((entry) => {
      const paired = eligible(entry,3);
      return {entry, gain:paired.length ? mean(paired.map((run) => score(run,3)-score(run,1))) : null};
    }).filter((item) => item.gain != null);
    if (gains.length) {
      const best = Math.max(...gains.map((item) => item.gain));
      const winners = gains.filter((item) => Math.abs(item.gain-best)<1e-9).map((item) => entryTitle(item.entry));
      notes.push(best > 1e-9
        ? `Largest estimated retry gain: ${delta(0,best)} from pass@1 to pass@3 (${winners.join(", ")}), using matched runs with at least three attempts per case. This does not diagnose the cause of failures.`
        : "The eligible runs show no observed increase from pass@1 to pass@3. This sample does not establish that retries never help.");
    } else notes.push("Retry gains are unavailable until a complete run has at least three attempts per case.");
    part("findings").replaceChildren(...notes.map((text) => node("li",text)));
  }

  function speedChart(target, entries) {
    const measured = entries.filter((entry) => wallTime(entry) != null && average(entry,1) != null);
    const width = window.innerWidth < 640 ? 350 : 700;
    const left = 43, right = width-24, top = 30, bottom = 225;
    const max = measured.length ? Math.max(...measured.map(wallTime))*1.15 : 1;
    const x = (value) => left + value/max*(right-left), y = (value) => bottom-value*(bottom-top);
    const svg = svgNode("svg",{viewBox:`0 0 ${width} 275`,role:"img","aria-label":"pass@1 versus evaluation seconds per attempt. Each model line connects its measured thinking levels in level order. Exact values and missing levels are listed below."});
    for (const value of [0,.25,.5,.75,1]) {
      svg.append(svgNode("line",{x1:left,x2:right,y1:y(value),y2:y(value),stroke:"#e1e6ed"}));
      svg.append(svgNode("text",{x:left-7,y:y(value)+4,"text-anchor":"end"},`${value*100}%`));
    }
    for (let i=0;i<=4;i++) {
      const value=i*max/4;
      svg.append(svgNode("text",{x:x(value),y:bottom+20,"text-anchor":"middle"},`${value.toFixed(1)}s`));
    }
    svg.append(svgNode("text",{x:left,y:14},"pass@1"), svgNode("text",{x:(left+right)/2,y:270,"text-anchor":"middle"},"Evaluation seconds / attempt (lower is faster)"));
    const series = new Map();
    for (const entry of entries) {
      const key = entry.run.configuration_key;
      if (!series.has(key)) series.set(key,[]);
      series.get(key).push(entry);
    }
    const legend = node("ul");
    const colors = ["#376d9e", "#c47a22", "#22654c", "#88549f", "#a13730", "#317c88", "#786126"];
    const models = [...new Set(runs.map((run) => run.config.model))].sort();
    const revisions = new Map(), labelBoxes = [];
    const labels = svgNode("g");
    for (const family of series.values()) {
      const first = family[0], model = first.run.config.model;
      const color = colors[models.indexOf(model) % colors.length];
      const revision = revisions.get(model) || 0;
      revisions.set(model,revision+1);
      const dash = ["none", "6 3", "2 3", "8 3 2 3"][revision % 4];
      const advertised = first.run.config.thinking_levels || [];
      const order = (entry) => entry.level == null ? Infinity
        : typeof entry.level === "boolean" ? Number(entry.level)
        : advertised.includes(entry.level) ? advertised.indexOf(entry.level) : advertised.length;
      const ordered = [...family].sort((a,b) => order(a)-order(b));
      const label = `${model}${first.revisionLabel ? ` · ${first.revisionLabel}` : ""}`;
      const group = svgNode("g",{"data-model":model,"data-configuration":first.run.configuration_key});
      group.append(svgNode("title",{},label));
      let segment = [];
      const flush = () => {
        if (segment.length > 1) group.append(svgNode("polyline",{points:segment.join(" "),fill:"none",stroke:color,"stroke-width":2,"stroke-dasharray":dash}));
        segment = [];
      };
      for (const entry of ordered) {
        if (entry.level == null || wallTime(entry) == null || average(entry,1) == null) { flush(); continue; }
        segment.push(`${x(wallTime(entry))},${y(average(entry,1))}`);
      }
      flush();
      for (const entry of ordered) {
        const elapsed = wallTime(entry), quality = average(entry,1);
        if (elapsed == null || quality == null) continue;
        const cx=x(elapsed), cy=y(quality), level=thinking(entry.level);
        const description = `${entryTitle(entry)} · ${pct(quality)} · ${seconds(elapsed)}/attempt`;
        const circle=svgNode("circle",{cx,cy,r:5,fill:entry.level === false ? "white" : color,stroke:color,"stroke-width":1.7,"data-thinking":level});
        circle.append(svgNode("title",{},description));
        group.append(circle);
        const textWidth = Math.min(right-left,level.length*6);
        let lx, ly;
        for (const offset of [-10,18,-26,34,-42,50]) {
          lx = Math.max(left,Math.min(right-textWidth,cx+9));
          ly = Math.max(14,Math.min(bottom+12,cy+offset));
          if (!labelBoxes.some(([px,py,pw]) => lx < px+pw+5 && lx+textWidth+5 > px && Math.abs(ly-py)<14)) break;
        }
        labelBoxes.push([lx,ly,textWidth]);
        if (Math.abs(ly-cy)>20) labels.append(svgNode("line",{x1:cx,y1:cy,x2:lx,y2:ly-3,stroke:color,"stroke-width":.7}));
        labels.append(svgNode("text",{x:lx,y:ly,stroke:"white","stroke-width":3,"paint-order":"stroke",style:`fill:${color}`},level));
      }
      svg.append(group);
      const item = node("li");
      item.append(node("strong",label),node("div",ordered.map((entry) => {
        const quality = average(entry,1), elapsed = wallTime(entry);
        const value = !entry.runs.length ? "Not run" : quality == null ? "Unavailable"
          : `${pct(quality)} · ${elapsed == null ? "time unavailable" : `${seconds(elapsed)}/attempt`}`;
        return `${thinking(entry.level)}: ${value}`;
      }).join("; ")));
      item.style.setProperty("--series", color);
      item.style.setProperty("--line-style", revision % 4 === 0 ? "solid" : "dashed");
      legend.append(item);
    }
    svg.append(labels);
    if (measured.length) target.append(svg);
    else target.append(node("p","No complete timed runs yet.","caption"));
    target.append(legend);
  }

  function heatmap(table, entries, cases, inspect) {
    header(table,["Model / thinking",...cases.map((spec)=>spec.label)]);
    for (const entry of entries) {
      const row=node("tr"), label=cell(row);
      row.dataset.thinking=thinking(entry.level);
      const heading=node(entry.runs.length ? "button" : "strong",entry.run.config.model);
      if (entry.runs.length) heading.addEventListener("click",()=>inspect(entry.runs.at(-1)));
      label.append(heading,node("div",`Thinking ${thinking(entry.level)}${entry.revisionLabel ? ` · ${entry.revisionLabel}` : ""}`,"caption"));
      for (const spec of cases) {
        const td=cell(row);
        const scores=[];
        for (const [k] of metricColors) {
          const values=eligible(entry,k).map((run)=>run.cases.find((item)=>item.id===spec.id)?.scores?.[k]).filter((value)=>value!=null);
          const value=mean(values);
          scores.push(entry.runs.length ? pct(value) : "Not run");
          if (k===1 && value!=null) td.style.backgroundColor=`hsl(${Math.round(12+value*130)} 38% 92%)`;
        }
        td.textContent=scores.join(" / ");
        td.setAttribute("aria-label",`${spec.label} · pass@1: ${scores[0]} · pass@3: ${scores[1]}`);
      }
      table.querySelector("tbody").append(row);
    }
  }

  function benchmark(items, number) {
    const section = byId("benchmark-section").content.firstElementChild.cloneNode(true);
    const part = (role) => section.querySelector(`[data-role="${role}"]`);
    const last = items.at(-1);
    part("progress").id=`history-${last.comparison_key}`;
    if (number===1) byId("history-link").href=`#${part("progress").id}`;
    const ks = [...new Set([1, 3, ...items.flatMap((r) => r.config.ks)])].sort((a, b) => a-b);
    const judged = last.cases.some((c) => c.group === "judged");
    part("suite-label").textContent = `Eval suite ${number} · ${short(last.comparison_key)}${last.benchmark_version ? "" : " · legacy"}`;
    part("title").textContent = `${countOf(last.cases.length,"case")} · ${judged ? "Objective checks + rubric judge" : "Objective checks"}`;
    part("series-note").textContent = `${countOf(items.length,"run")} · ${date(items[0])} through ${date(last)}${judged ? ` · Judge: ${last.config.judge_model} (${last.config.judge_provider || "legacy provider"}, thinking ${thinking(last.config.judge_thinking)})` : ""}. Same cases and grading; models, thinking, prompts, tools, and harness can change.`;
    for (const k of ks) {
      const complete = items.filter((r) => score(r, k) != null);
      const first = complete[0], latest = complete.at(-1);
      const best = complete.reduce((winner, run) => !winner || score(run, k) > score(winner, k) ? run : winner, null);
      const card = node("section");
      card.dataset.k = String(k);
      card.append(node("div", `pass@${k}`, "eyebrow"), node("strong", pct(score(latest, k))),
        node("p", latest ? `Latest complete · ${latest.config.model} · ${thinking(latest.config.thinking)}` : `Needs a complete run with at least ${k} attempts per case.`),
        node("p", complete.length > 1 ? `${delta(score(first,k), score(latest,k))} since first complete · baseline ${pct(score(first,k))} · best ${pct(score(best,k))}` : complete.length ? "First complete result; evaluate again to measure change." : "Missing results are not zero.", "caption"));
      if (latest && latest !== last) card.append(node("p", "Newer runs have no complete score for this metric.", "caption"));
      part("metrics").append(card);
    }

    function inspect(run, scroll = true) {
      part("run-title").textContent = `Run details · ${name(run)} · ${thinking(run.config.thinking)}`;
      part("run-summary").textContent = `${date(run)} · ${run.status} · ${countOf(run.cases.length,"case")} × ${countOf(run.config.samples,"attempt")} · ${run.duration_seconds.toFixed(1)} seconds`;
      const metadata = [
        ["Run ID", run.run_id], ["Experiment", run.experiment || "None"],
        ["Model", run.config.model], ["Thinking", thinking(run.config.thinking)],
        ["Supported levels", run.config.thinking_levels?.map(thinking).join(", ") || "Not recorded"],
        ["Configuration", run.revision ? short(run.configuration_key) : "Incomplete provenance"],
        ["System prompt", short(run.agent_hash)], ["Runtime / tools", short(run.revision?.source_hash)],
        ["Dependencies", short(run.revision?.dependencies_hash)], ["Harness", versions(run) || "Not recorded"],
        ["Git revision", run.revision?.commit || "Not recorded"],
        ["Local changes", run.revision?.dirty == null ? "Not recorded" : run.revision.dirty ? "Yes" : "No"],
        ["Temperature", run.config.temperature], ["Answer timeout", `${run.config.timeout_seconds} seconds`],
        ["Judge model", run.config.judge_model || "Not used"],
        ["Judge provider", run.config.judge_provider || (run.config.judge_model ? "Not recorded (legacy)" : "Not used")],
        ["Judge thinking", run.config.judge_model ? thinking(run.config.judge_thinking) : "Not used"],
        ["Judge temperature", run.config.judge_model ? run.config.judge_temperature : "Not used"],
      ];
      part("provenance").replaceChildren(...metadata.flatMap(([label, value]) => [node("dt", label), node("dd", String(value))]));
      const table = part("cases");
      header(table, ["Case", "Grading", ...ks.map((k) => `pass@${k}`), "Pass / fail", "Unscored", "Errors: agent / grader"]);
      table.querySelector("tbody").replaceChildren(...run.cases.map((item) => {
        const row = node("tr");
        cell(row, item.label); cell(row, item.group);
        for (const k of ks) cell(row, pct(item.scores?.[k]), true);
        cell(row, `${item.passed} / ${item.failed}`); cell(row, String(item.unscored));
        cell(row, `${item.generation_errors} / ${item.grading_errors}`);
        return row;
      }));
      if (scroll) {
        part("run-details").open = true;
        part("run-details").scrollIntoView({behavior:window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block:"start"});
      }
    }
    const button = (run) => {
      const result = node("button", name(run));
      result.title = title(run);
      result.addEventListener("click", () => inspect(run));
      return result;
    };
    const families = new Map();
    for (const run of items) {
      if (!families.has(run.configuration_key)) families.set(run.configuration_key, []);
      families.get(run.configuration_key).push(run);
    }
    const board = part("board");
    header(board, ["Model / configuration", "Thinking", ...ks.map((k) => `pass@${k}`), "Eval / attempt", "Runs / samples"]);
    header(part("models"),["Model", "Thinking levels", "Configuration", "Generation settings"]);
    const entries = [];
    let missing = 0, expected = 0, unknown = 0;
    for (const family of families.values()) {
      const newest = family.at(-1);
      const repeatedModel = [...families.values()].filter((other) => other[0].config.model === newest.config.model).length > 1;
      const levels = new Map();
      for (const run of family) for (const level of run.config.thinking_levels || []) levels.set(levelKey(level), level);
      expected += levels.size;
      if (!levels.size) unknown++;
      for (const key of levels.keys()) if (!family.some((r) => levelKey(r.config.thinking) === key)) missing++;
      for (const run of family) levels.set(levelKey(run.config.thinking), run.config.thinking);
      const modelRow=node("tr");
      cell(modelRow,newest.config.model);
      cell(modelRow,[...levels.values()].map(thinking).join(", "));
      cell(modelRow,newest.revision ? short(newest.configuration_key) : "Incomplete provenance").append(node("div",newest.label || "Unlabeled","caption"));
      cell(modelRow,`Temperature ${newest.config.temperature}`).append(node("div",`${newest.config.timeout_seconds}s answer timeout`,"caption"));
      part("models").querySelector("tbody").append(modelRow);
      for (const [key, level] of levels) {
        const matching = family.filter((r) => levelKey(r.config.thinking) === key);
        const latest = matching.at(-1);
        const entry = {run:newest,level,runs:matching,revisionLabel:repeatedModel ? newest.label || date(newest) : ""};
        entries.push(entry);
        const row = node("tr");
        row.dataset.thinking = thinking(level);
        const label = cell(row);
        label.append(node("strong", newest.config.model), node("div", newest.label || "Unlabeled configuration", "caption"),
          node("div", newest.revision ? short(newest.configuration_key) : "Incomplete provenance · ungrouped", "caption"));
        cell(row, thinking(level));
        for (const k of ks) {
          const scored = matching.filter((r) => score(r,k) != null);
          const mean = scored.length ? scored.reduce((sum, r) => sum + score(r,k), 0) / scored.length : null;
          const td = cell(row, undefined, true);
          td.append(node("strong", matching.length ? pct(mean) : "Not run"));
          if (scored.length) td.append(node("div", `${scored.length} complete · latest ${pct(score(scored.at(-1),k))}`, "caption"));
          else if (matching.length) td.append(node("div", "Incomplete or insufficient samples", "caption"));
        }
        cell(row,seconds(wallTime(entry)),true);
        const count = cell(row, countOf(matching.length,"run"));
        if (matching.length) count.append(node("div",`${[...new Set(matching.map((run)=>run.config.samples))].sort((a,b)=>a-b).join(", ")} attempts/case`,"caption"));
        count.append(node("div"));
        if (latest) count.append(status(latest.status)); else count.append(status("not run"));
        board.querySelector("tbody").append(row);
      }
    }
    part("coverage").textContent = `${expected} advertised configuration/level combinations; ${missing} not run.${unknown ? ` ${unknown} configurations have no recorded thinking-level metadata; their coverage is unknown.` : ""} Historical defaults are not counted as an explicit level.`;
    findings(part,entries,items);
    pairedBars(part("paired-bars"),entries,inspect);
    speedChart(part("speed"),entries);
    window.matchMedia?.("(max-width: 639px)").addEventListener("change", () => {
      part("speed").replaceChildren();
      speedChart(part("speed"),entries);
      part("paired-bars").replaceChildren();
      pairedBars(part("paired-bars"),entries,inspect);
    });
    heatmap(part("heatmap"),entries,last.cases,inspect);
    for (const [, label, color] of groups) {
      const item = node("span", label);
      item.style.setProperty("--series", color);
      part("legend").append(item);
    }
    for (const k of ks) {
      const panel = node("section");
      panel.dataset.k = String(k);
      panel.append(node("h3", `pass@${k}`));
      chart(panel, items, k, inspect);
      part("charts").append(panel);
    }
    const history = part("history");
    header(history, ["Run / change", "Model / thinking", "Recorded changes", ...ks.map((k) => `pass@${k}`), "Status"]);
    items.forEach((run, index) => {
      const row = node("tr"), previous = items[index-1];
      const label = cell(row);
      label.append(button(run), node("div", date(run), "caption"));
      if (run.experiment) label.append(node("div", `Experiment: ${run.experiment}`, "caption"));
      cell(row, run.config.model).append(node("div", thinking(run.config.thinking), "caption"));
      const changed = previous ? differences(previous,run).join(", ") || "Same recorded settings" : "First run";
      cell(row, changed).append(node("div", !run.revision || run.config.thinking == null ? "Some settings unrecorded" : "", "caption"));
      for (const k of ks) cell(row, pct(score(run,k)), true).append(change(score(previous,k),score(run,k)));
      cell(row).append(status(run.status));
      history.querySelector("tbody").append(row);
    });
    for (const role of ["baseline", "candidate"]) {
      part(role).replaceChildren(...items.map((run) => {
        const option = node("option", title(run)); option.value = run.run_id; return option;
      }));
    }
    const complete = items.filter((r) => score(r,1) != null);
    part("baseline").value = (complete[0] || items[0]).run_id;
    part("candidate").value = (complete.at(-1) || last).run_id;
    function compare() {
      const before = items.find((r) => r.run_id === part("baseline").value);
      const after = items.find((r) => r.run_id === part("candidate").value);
      const changed = differences(before,after);
      let note;
      if (before === after) note = "Select two different runs to compare.";
      else if (!before.revision || !after.revision || before.config.thinking == null || after.config.thinking == null) note = "Some settings, including thinking defaults or provenance, were not explicitly recorded. A controlled comparison cannot be verified.";
      else if (changed.length === 1) note = `Only ${changed[0].toLowerCase()} differs in the recorded settings. Repeat the comparison to assess sampling variation.`;
      else if (changed.length === 0) note = "Same recorded configuration and sample budget; differences reflect repeat variability or unrecorded factors.";
      else note = `Multiple settings differ: ${changed.join(", ")}. The score change cannot be attributed to one of them.`;
      part("comparison-note").textContent = note;
      const table = part("comparison-scores");
      header(table, ["Metric", "Baseline", "Candidate", "Change"]);
      table.querySelector("tbody").replaceChildren(...ks.map((k) => {
        const row = node("tr"); cell(row, `pass@${k}`); cell(row,pct(score(before,k))); cell(row,pct(score(after,k)));
        cell(row).append(change(score(before,k),score(after,k))); return row;
      }));
      part("settings").querySelector("tbody").replaceChildren(...settings.map(([label,value,format]) => {
        const row = node("tr"), a = value(before), b = value(after);
        cell(row,label);
        for (const item of [a,b]) {
          const td = cell(row, item == null ? "Not explicitly recorded" : format ? format(item) : String(item));
          if (item != null) td.title = String(item);
        }
        const td = cell(row, a == null || b == null ? "Unknown" : a === b ? "Same" : "Changed");
        if (a != null && b != null && a !== b) td.dataset.changed = "true";
        return row;
      }));
      const cases = part("deltas");
      header(cases, ["Case", ...ks.map((k) => `pass@${k} · baseline → candidate`)]);
      cases.querySelector("tbody").replaceChildren(...before.cases.map((a) => {
        const b = after.cases.find((item) => item.id === a.id), row = node("tr");
        cell(row,a.label);
        for (const k of ks) cell(row, `${pct(a.scores?.[k])} → ${pct(b?.scores?.[k])}`,true).append(change(a.scores?.[k],b?.scores?.[k]));
        return row;
      }));
    }
    part("baseline").addEventListener("change",compare);
    part("candidate").addEventListener("change",compare);
    compare();
    inspect(last,false);
    return section;
  }
  byId("run-count").textContent = countOf(runs.length,"saved evaluation run");
  byId("empty").hidden = runs.length !== 0;
  const suites = new Map();
  for (const run of runs) {
    if (!suites.has(run.comparison_key)) suites.set(run.comparison_key, []);
    suites.get(run.comparison_key).push(run);
  }
  byId("report-summary").textContent = runs.length ? `${countOf(suites.size,"eval suite")} shown below, newest first. Pass@1, pass@3, and other recorded budgets are visible together. Suites with different cases or grading are kept separate.` : "No benchmark results yet.";
  const ordered = [...suites.values()].sort((a,b) => new Date(b.at(-1).started_at) - new Date(a.at(-1).started_at));
  ordered.forEach((items,index) => byId("benchmarks").append(benchmark(items,index+1)));
})();

/* Plluminati UI.
 *
 * Two rules shape this file:
 *   1. The keyboard diagram is the hero. Everything else is peripheral, because
 *      the player is looking at their hands, not the screen.
 *   2. Colour is never the only signal - state changes also carry a mark and a
 *      motion, so it reads at a metre and satisfies WCAG 1.4.1.
 */

const LOW = 36, HIGH = 96;
const BLACK = new Set([1, 3, 6, 8, 10]);
const NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
const noteName = n => NAMES[n % 12] + (Math.floor(n / 12) - 1);
const pct = v => Math.round(v * 100) + "%";

const SCREENS = ["profiles","newprofile","home","songs","wizard","wzdone","play","done"];
const $ = id => document.getElementById(id);
const show = id => {
  SCREENS.forEach(s => $(s).classList.toggle("hidden", s !== id));
  $("backbtn").classList.toggle("hidden", id === "profiles" || id === "play");
  current = id;
};
let current = "profiles";

async function cmd(command, args = {}) {
  const r = await fetch("/command", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({command, args})
  });
  return r.json();
}

/* ------------------------------------------------------------- keyboard */

const keyEls = new Map();
let markLayer = null;

function buildKeyboard() {
  const svg = $("keys");
  svg.innerHTML = "";
  const whites = [];
  for (let n = LOW; n <= HIGH; n++) if (!BLACK.has(n % 12)) whites.push(n);
  const W = 1000 / whites.length, H = 150, BW = W * 0.62, BH = H * 0.62;
  const mk = (tag, attrs) => {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };
  whites.forEach((n, i) => {
    const r = mk("rect", {x: i*W, y: 0, width: W-1, height: H, rx: 3,
                          class: "key wkey"});
    svg.appendChild(r); keyEls.set(n, r);
  });
  whites.forEach((n, i) => {
    const nxt = n + 1;
    if (nxt <= HIGH && BLACK.has(nxt % 12)) {
      const r = mk("rect", {x: i*W + W - BW/2, y: 0, width: BW, height: BH,
                            rx: 2, class: "key bkey"});
      svg.appendChild(r); keyEls.set(nxt, r);
    }
  });
  markLayer = mk("g", {});
  svg.appendChild(markLayer);
}

const setKey = (note, cls, on) => {
  const el = keyEls.get(note);
  if (el) el.classList.toggle(cls, !!on);
};

function flash(note, good) {
  const el = keyEls.get(note);
  if (!el) return;
  el.classList.add(good ? "good" : "bad", good ? "pop" : "shake");
  const box = el.getBBox();
  const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
  t.setAttribute("x", box.x + box.width/2);
  t.setAttribute("y", box.y + box.height - 12);
  t.setAttribute("class", "mark " + (good ? "good" : "bad"));
  t.textContent = good ? "✓" : "✕";
  markLayer.appendChild(t);
  setTimeout(() => {
    el.classList.remove("good","bad","pop","shake"); t.remove();
  }, good ? 420 : 520);
}

const clearAll = () => {
  keyEls.forEach(el => el.classList.remove("cue","down","good","bad"));
  if (markLayer) markLayer.innerHTML = "";
};

/* ------------------------------------------------------------------ SSE */

let streak = 0, wrong = 0, mode = "learn", lastStart = null, profile = null;

function connect() {
  const es = new EventSource("/events");
  es.onmessage = ev => handle(JSON.parse(ev.data));
  es.onerror = () => {
    $("livetext").textContent = "reconnecting…";
    $("livedot").classList.remove("live");
  };
}

function handle(e) {
  switch (e.type) {
    case "live":
      $("livedot").classList.toggle("live", e.live);
      $("livetext").textContent = e.live ? "keyboard ready" : "press any key to wake";
      break;

    case "played": setKey(e.note, "down", e.on); break;

    case "cue":
      (e.notes || []).forEach(n => setKey(n, "cue", e.on));
      break;

    case "wake_needed":
      $("p-notes").textContent = "Press any key to wake the keyboard";
      $("p-sub").textContent = e.message || "";
      break;

    case "wizard":
      show("wizard");
      $("wz-title").textContent = e.title;
      $("wz-detail").textContent = e.detail || "";
      $("wz-hint").classList.toggle("hidden", !e.answer);
      $("wz-buttons").classList.toggle("hidden", !e.answer);
      keyEls.forEach(el => el.classList.remove("cue"));
      (e.cue || []).forEach(n => setKey(n, "cue", true));
      break;

    case "wizard_done": {
      show("wzdone"); clearAll();
      const box = $("wz-results"); box.innerHTML = "";
      const label = {
        lightable_range: "Are both end keys lit?",
        note_off_cuts_held: "Does our note-off cut your held note?",
        cues_steal_voices: "Do cue lights steal polyphony voices?"
      };
      for (const [k, v] of Object.entries(e.results || {})) {
        const row = document.createElement("div");
        row.className = "result";
        const a = v.value === true ? "yes" : v.value === false ? "no" : "skip";
        row.innerHTML = `<span class="q"><b>${label[k] || k}</b><br>
          <span class="meta">${v.detail || ""}</span></span>
          <span class="a ${a}">${a.toUpperCase()}</span>`;
        box.appendChild(row);
      }
      break;
    }

    case "started":
      mode = e.mode;
      show("play"); clearAll();
      streak = 0; wrong = 0;
      $("p-streak").textContent = "0";
      $("p-wrong").textContent = "0";
      $("p-progress-k").textContent = mode === "drill" ? "Hits" : "Step";
      $("p-speed").textContent = mode === "drill" ? "…" : "wait";
      $("p-sub").textContent = `${e.title} · ${e.hand} hand`
        + (e.bars ? ` · bars ${e.bars[0]}–${e.bars[1]}` : "")
        + ` · hands by ${e.detection} (${e.confidence})`;
      break;

    case "step":
      keyEls.forEach(el => el.classList.remove("cue"));
      (e.cue || []).forEach(n => setKey(n, "cue", true));
      $("p-bar").textContent = e.bar;
      $("p-progress").textContent = `${e.done}/${e.total}`;
      $("p-notes").textContent = (e.pitches || []).map(noteName).join("  ");
      break;

    case "hint":
      // A repeated note cannot be re-lit while held, so say so rather than
      // showing an apparently stalled screen.
      $("p-sub").textContent = e.kind === "release"
        ? "Let go and play it again — same key"
        : $("p-sub").textContent;
      $("p-notes").classList.toggle("dim", e.kind === "release");
      break;

    case "verdict":
      flash(e.note, e.ok);
      if (e.ok) streak++; else { streak = 0; wrong++; $("p-wrong").textContent = wrong; }
      $("p-streak").textContent = streak;
      break;

    case "rep_start":
      $("p-speed").textContent = pct(e.speed);
      $("p-notes").textContent = `Rep ${e.rep}`;
      streak = 0; wrong = 0;
      $("p-wrong").textContent = "0"; $("p-streak").textContent = "0";
      break;

    case "rep_end":
      $("p-progress").textContent = `${e.hits}/${e.targets}`;
      $("p-notes").textContent =
        e.verdict === "clean" ? "Clean!" : e.verdict === "poor" ? "Slowing down" : "Hold";
      break;

    case "listen_done":
      payoff({title: "That's the tune", a: [`${e.notes}`, "Notes"],
              b: [`${e.seconds}s`, "Length"], c: ["—", ""], next: "Again"});
      break;

    case "learn_done":
      payoff({
        title: e.clean === e.total && e.total ? "Clean!" : "Nice work",
        a: [`${e.clean}`, "Clean first time"],
        b: [`${e.wrong}`, "Wrong notes"],
        c: [`${Math.round(e.seconds)}s`, "Time"],
        next: "Again"
      });
      break;

    case "drill_done":
      payoff({
        title: e.best_clean ? "Clean!" : "Good run",
        jump: [e.start_speed, e.speed],
        a: [`${e.hits}/${e.targets}`, "Notes"],
        b: [`${e.wrong}`, "Wrong"],
        c: [`±${e.timing_ms} ms`, "Timing"],
        next: e.speed > e.start_speed ? `Play it at ${pct(e.speed)} →` : "Again",
        speed: e.speed
      });
      break;
  }
}

/* The end of a run is a moment, not a report. */
function payoff(o) {
  show("done"); clearAll();
  $("d-title").textContent = o.title;
  const jump = $("d-jump");
  if (o.jump && o.jump[1] !== o.jump[0]) {
    jump.classList.remove("hidden");
    $("d-from").textContent = pct(o.jump[0]);
    const to = $("d-to");
    to.textContent = pct(o.jump[1]);
    to.classList.remove("roll"); void to.offsetWidth; to.classList.add("roll");
  } else jump.classList.add("hidden");
  $("d-a").textContent = o.a[0]; $("d-ak").textContent = o.a[1];
  $("d-b").textContent = o.b[0]; $("d-bk").textContent = o.b[1];
  $("d-c").textContent = o.c[0]; $("d-ck").textContent = o.c[1];
  $("btn-next").textContent = o.next;
  nextSpeed = o.speed || null;
}
let nextSpeed = null;

/* -------------------------------------------------------------- screens */

async function loadProfiles() {
  const r = await cmd("profiles");
  const box = $("profiletiles"); box.innerHTML = "";
  (r.profiles || []).forEach(p => {
    const b = document.createElement("button");
    b.className = "tile";
    b.style.borderColor = p.accent;
    b.innerHTML = `<span class="face">${p.avatar}</span><span class="who">${p.name}</span>`;
    b.onclick = async () => {
      const res = await cmd("profile.select", {id: p.id});
      if (res.ok) applyProfile(res.profile);
    };
    box.appendChild(b);
  });
  show("profiles");
}

function applyProfile(p) {
  profile = p;
  document.documentElement.style.setProperty("--accent", p.accent);
  $("whoami").textContent = `${p.avatar} ${p.name}`;
  $("whoami").classList.remove("hidden");
  $("home-greet").textContent = `Ready when you are, ${p.name}`;
  hand = p.prefs?.hand || "right";
  document.querySelectorAll(".hand").forEach(x =>
    x.classList.toggle("primary", x.dataset.hand === hand));
  show("home");
}

let avatars = [], themes = {}, pickAvatar = "", pickTheme = "";

function buildNewProfile() {
  const av = $("np-avatars"); av.innerHTML = "";
  avatars.forEach(a => {
    const b = document.createElement("button");
    b.className = "opt"; b.textContent = a;
    b.onclick = () => { pickAvatar = a;
      av.querySelectorAll(".opt").forEach(x => x.classList.toggle("sel", x === b)); };
    av.appendChild(b);
  });
  const th = $("np-themes"); th.innerHTML = "";
  Object.entries(themes).forEach(([name, hex]) => {
    const b = document.createElement("button");
    b.className = "swatch"; b.style.background = hex;
    b.onclick = () => { pickTheme = name;
      th.querySelectorAll(".swatch").forEach(x => x.classList.toggle("sel", x === b)); };
    th.appendChild(b);
  });
  $("np-name").value = ""; pickAvatar = ""; pickTheme = "";
  show("newprofile");
  setTimeout(() => $("np-name").focus(), 50);
}

let hand = "right", lastSong = null, lastBars = null;
let wrongSound = "hihat", wrongSounds = [], djMode = false;

async function loadSongs(which) {
  mode = which || "learn";
  $("lib-title").textContent = mode === "drill" ? "Drill a section"
    : mode === "listen" ? "Listen to a song" : "Choose a song";
  const r = await cmd("songs");
  const list = $("songlist"); list.innerHTML = "";
  (r.songs || []).forEach(s => {
    const b = document.createElement("button");
    b.className = "song";
    const levels = s.mastery_levels || 5;
    const ring = (lbl, n) => {
      const on = "●".repeat(n), off = "○".repeat(levels - n);
      return `<span class="ring"><span class="pips">${on}<span class="off">${off}</span></span>
              <span class="lbl">${lbl}</span></span>`;
    };
    const m = s.mastery || {};
    const rings = `<span class="rings">${ring("Right", m.right||0)}
                   ${ring("Left", m.left||0)}${ring("Both", m.both||0)}</span>`;
    const c = s.continue;
    const cont = c ? `<br><span class="cont">Continue: ${c.hand} hand at ${pct(c.speed)}`
        + (c.section ? ` · bars ${c.section[0]}–${c.section[1]}` : "") + `</span>` : "";
    const warn = s.needs_check ? `<span class="warn"> · hands uncertain</span>` : "";
    const oor = s.out_of_range ? `<span class="warn"> · ${s.out_of_range} out of range</span>` : "";
    b.innerHTML = `<span style="flex:1"><span class="name">${s.name}</span><br>
      <span class="meta">${s.steps ?? "?"} steps · ${s.bars ?? "?"} bars${warn}${oor}</span>
      ${cont}</span>${rings}`;
    b.onclick = () => { lastSong = s.path; lastBars = null; startPractice(); };
    list.appendChild(b);
  });
  show("songs");
}

async function startPractice(speed) {
  const args = {path: lastSong, hand};
  if (lastBars) args.bars = lastBars;
  args.wrong_sound = wrongSound;
  args.dj_mode = djMode;
  let command = "learn.start";
  if (mode === "drill") { command = "drill.start"; args.speed = speed || 0.6; }
  else if (mode === "listen") { command = "listen.start"; args.hand = null; }

  const r = await cmd(command, args);
  if (!r.ok) alert(r.error || "could not start");
}

/* ---------------------------------------------------------------- wiring */

document.addEventListener("DOMContentLoaded", async () => {
  buildKeyboard();
  connect();
  const hello = await cmd("hello");
  avatars = hello.avatars || []; themes = hello.themes || {};
  $("livedot").classList.toggle("live", !!hello.live);
  $("livetext").textContent = hello.live ? "keyboard ready" : "press any key to wake";
  if (hello.profile) applyProfile(hello.profile); else loadProfiles();

  $("whoami").onclick = loadProfiles;
  $("btn-newprofile").onclick = buildNewProfile;
  $("np-cancel").onclick = loadProfiles;
  $("np-create").onclick = async () => {
    const name = $("np-name").value.trim();
    if (!name) return $("np-name").focus();
    const r = await cmd("profile.create",
                        {name, avatar: pickAvatar, theme: pickTheme});
    if (r.ok) applyProfile(r.profile);
  };
  $("np-name").addEventListener("keydown", e => {
    if (e.key === "Enter") $("np-create").click();
  });

  $("btn-learn").onclick = () => loadSongs("learn");
  $("btn-drill").onclick = () => loadSongs("drill");
  $("btn-listen").onclick = () => loadSongs("listen");
  $("btn-pick").onclick = () => loadSongs(mode);
  $("btn-after-wizard").onclick = () => loadSongs("learn");
  wrongSounds = hello.wrong_sounds || [];
  wrongSound = hello.wrong_sound || "hihat";

  // The wrong-note sound is a taste decision, so make it auditionable:
  // clicking a chip plays it immediately rather than describing it.
  const LABEL = {hihat:"hi-hat", stick:"click", clap:"clap", thud:"thud",
                 kick:"kick", scratch:"scratch", cowbell:"cowbell",
                 crash:"crash"};
  const pick = $("wrongpick");
  const paintPicks = () => {
    pick.innerHTML = "";

    wrongSounds.forEach(name => {
      const b = document.createElement("button");
      b.className = "chip" + (name === wrongSound ? " sel" : "");
      b.textContent = LABEL[name] || name;
      b.title = "wrong-note sound - click to hear it";
      b.onclick = async () => {
        wrongSound = name;
        await cmd("wrong.sound", {name});     // the server previews it
        const paintDj = () => {
    const b = $("djtoggle");
    b.textContent = "DJ mode: " + (djMode ? "ON" : "off");
    b.classList.toggle("ghost", !djMode);
    $("wrongpick").classList.toggle("hidden", djMode);
    $("lib-title").textContent = djMode
      ? "Press DJ (or pick voice 98) on the keyboard"
      : $("lib-title").textContent;
  };
  paintPicks(); paintDj();
  $("djtoggle").onclick = () => { djMode = !djMode; paintDj(); };
      };
      pick.appendChild(b);
    });
  };
  const paintDj = () => {
    const b = $("djtoggle");
    b.textContent = "DJ mode: " + (djMode ? "ON" : "off");
    b.classList.toggle("ghost", !djMode);
    $("wrongpick").classList.toggle("hidden", djMode);
    $("lib-title").textContent = djMode
      ? "Press DJ (or pick voice 98) on the keyboard"
      : $("lib-title").textContent;
  };
  paintPicks(); paintDj();
  $("djtoggle").onclick = () => { djMode = !djMode; paintDj(); };
  $("btn-next").onclick = () => startPractice(nextSpeed);
  $("btn-wizard").onclick = () => cmd("wizard.start");
  $("btn-stopplay").onclick = () => { cmd("stop"); clearAll(); show("home"); };
  $("backbtn").onclick = () => {
    cmd("stop"); clearAll();
    show(current === "home" ? "profiles" : "home");
  };

  document.querySelectorAll("#wz-buttons button").forEach(b => {
    b.onclick = () => {
      const v = b.dataset.answer;
      cmd(v === "skip" ? "wizard.skip" : "wizard.answer", {value: v === "true"});
    };
  });
  document.querySelectorAll(".hand").forEach(b => {
    b.onclick = () => {
      hand = b.dataset.hand;
      document.querySelectorAll(".hand").forEach(x =>
        x.classList.toggle("primary", x === b));
    };
  });
});

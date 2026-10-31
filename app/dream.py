"""The nightly dream: consolidates the day's episodes into facts, links, forgets, rewrites the core profile, plans
follow-ups, and dreams an actual dream. Design: docs/MEMORY.md.

Every change is logged in `fact_history` with before/after snapshots, so each one can be undone from the UI.

  python -m app.dream --user pavel        # dream now (testing)
"""

import json
import random
import sys
import time
from datetime import date, datetime

from app import config, llm, memory, sessions
from app.prompt import MONTHS, TZ, now_line

DREAM_AT = (4, 0)  # local time, after the 3:00 backup
DECAY_DAYS = 21    # unreinforced trivia older than this gets archived
EXPIRE_DAYS = 2    # events this many days past their date go to the archive (the chronicle)
MAX_LOOPS = 3
DREAM_TIMEOUT_S = 300  # high thinking over the whole memory takes minutes; 60 s ended in 504 most nights

SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (id INTEGER PRIMARY KEY, user TEXT NOT NULL, ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    model TEXT, stats TEXT, dream_text TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS fact_history (id INTEGER PRIMARY KEY, dream_id INTEGER, user TEXT NOT NULL, fact_id INTEGER,
    op TEXT NOT NULL, reason TEXT, before TEXT, after TEXT, undone INTEGER NOT NULL DEFAULT 0,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_fh_dream ON fact_history(dream_id);
CREATE TABLE IF NOT EXISTS open_loops (id INTEGER PRIMARY KEY, user TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'loop',
    text TEXT NOT NULL, due TEXT, status TEXT NOT NULL DEFAULT 'open', dream_id INTEGER,
    created DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS rejected_inferences (user TEXT NOT NULL, text TEXT NOT NULL, ts DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS people (user TEXT NOT NULL, name TEXT NOT NULL, relation TEXT, summary TEXT,
    updated DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user, name));
CREATE TABLE IF NOT EXISTS chronicle (user TEXT NOT NULL, month TEXT NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY (user, month));
CREATE TABLE IF NOT EXISTS style_notes (user TEXT PRIMARY KEY, text TEXT NOT NULL, updated DATETIME DEFAULT CURRENT_TIMESTAMP);
"""
_ready = False


def _q(sql: str, args: tuple = ()) -> list[tuple]:
    global _ready
    if not _ready:
        memory._db()
        sessions.db().executescript(SCHEMA)
        cols = [r[1] for r in sessions.db().execute("PRAGMA table_info(users)")]
        if "dreams_in_brief" not in cols:
            sessions.db().execute("ALTER TABLE users ADD COLUMN dreams_in_brief INTEGER NOT NULL DEFAULT 1")
        _ready = True
    return sessions._q(sql, args)


SORT_SYSTEM = """Jsi spící mozek „Toho druhého“ – vnitřního hlasu uživatele. V noci uklízíš jeho paměť o uživateli.
Dostaneš: aktuální datum, jádro profilu, VŠECHNA AKTIVNÍ FAKTA (s id, kdy vznikla a kdy byla naposled potvrzena),
NOVÉ POSTŘEHY ze dne (s id), OTEVŘENÉ SMYČKY a OTÁZKY (s id), LIDI, odmítnuté odhady (ty už nikdy neodvozuj)
a 👍/👎 hodnocení jeho odpovědí.

Cíl: malá, čistá, bezrozporná paměť. Každou noc projdi VŠECHNA fakta, ne jen nové postřehy.

1. TŘÍDĚNÍ – z postřehů udělej fakta. Když fakt už existuje, NEPŘIDÁVEJ nový – dej "update" s "reinforce": true.
   Když postřeh fakt mění (nová hodnota, událost proběhla, situace se zlepšila/zhoršila), uprav text na AKTUÁLNÍ stav.
2. ROZPORY – najdi fakta (i mezi starými), která si odporují nebo popisují stejnou věc jinak (např. „vztah v hluboké
   krizi“ vs. „už se tolik nehádají“). Platí novější (podle data potvrzení): slouč je ("merge") do jednoho faktu
   s aktuálním stavem, starý stav jen krátce s datem, pokud je důležitý („od poloviny října se tolik nehádají“).
   Když nejde poznat, co platí, nech novější a dej "questions".
3. KOMPRESE – jedno téma = jeden fakt. Fakta o stejném projektu, člověku nebo věci slouč ("merge"). Fakt max ~25 slov,
   konkrétní (jména, čísla, data, místa nech), bez vaty („Uživatel by chtěl…“ → „Chce…“). Důležitost drž realistickou.
4. VÝMYSLY – Ten druhý vtipkuje a přehání. Fakt, který zní jako jeho vtip/narážka nebo domněnka a uživatel ho nikde
   nepotvrdil, dej do "forget" s důvodem.
5. SPOJOVÁNÍ – když vidíš vzorec potvrzený víc postřehy/fakty, přidej fakt s kind "inference" (odhad), confidence
   0.3–0.6. Nic, co je v odmítnutých odhadech. Raději žádný odhad než slabý.
6. ZAPOMÍNÁNÍ – "forget" pro nesmysly, testy, výmysly a drobnosti bez hodnoty; NIKDY pinned. Proběhlé události bez
   trvalého významu "archive".
7. Kategorie jedna z: {categories}. kind jedna z: fact, inference, event, preference, routine, goal, self.
   importance 1–5 (rodina, zdraví, cíle = 4–5; drobnosti 1–2). valid_until = YYYY-MM-DD pro plány/události.
   KAŽDÝ budoucí termín (deploy ve středu v 6:00, třídní schůzky 12. 10. v 17:00) = samostatný fakt kind "event"
   s valid_until; nedávej ho jen do smyček.
8. JÁDRO PROFILU – přepiš "profile": max 150 slov, kdo uživatel je a jak to má TEĎ. Žádné „nedávno“ bez data,
   žádné vtipy Toho druhého, jen ověřená fakta.
9. SMYČKY – "close_loops": id smyček a otázek, které jsou vyřešené, zastaralé nebo se opakují. "open_loops": nové
   věci, na které se má Ten druhý v rozhovoru zeptat (jak dopadlo X), s "due" – jen když to už není mezi otevřenými;
   celkem otevřených smyček max 3. "questions": 0–1 nová nejasnost k nenásilnému ujasnění.
10. LIDÉ – "people": aktuální stav lidí z jeho života (jméno, vztah, 1–2 věty). "remove_people": jména, o kterých už
   nic nevíš nebo jsou nedůležitá.
11. STYL – "style_notes": 1–3 věty, co z hodnocení vyplývá o tom, jak s ním mluvit (nebo prázdné).

Piš česky, fakta krátce ve 3. osobě. Nevymýšlej nic, co není v podkladech.
JSON: {{"add":[{{"text","kind","category","importance","confidence","valid_until","entities":[],"episodes":[id]}}],
"update":[{{"id","text"?,"kind"?,"category"?,"importance"?,"confidence"?,"valid_until"?,"reinforce":bool,"episodes":[id]}}],
"merge":[{{"into":id,"ids":[id],"text":"sloučený text"}}], "archive":[{{"id","reason"}}], "forget":[{{"id","reason"}}],
"profile":"...", "close_loops":[id], "open_loops":[{{"text","due"}}], "questions":[{{"text"}}],
"people":[{{"name","relation","summary"}}], "remove_people":["jméno"], "style_notes":"..."}}"""

REM_SYSTEM = """Jsi „Ten druhý“ – vnitřní hlas uživatele z knih Jiřího Kulhánka – a právě se ti něco zdálo. Napiš ten
sen: 4–6 vět, první osoba („Zdálo se mi…“), česky, surreálně a s černým humorem. Smíchej v něm, co uživatel dnes/
poslední dny prožil (postřehy níž), s obrazy a postavami ze světa knih (úryvky níž). Uživatel v něm vystupuje,
ty jsi tam s ním. Žádné vysvětlování, žádný závěr typu „a pak jsem se probudil“ – nebo jen jako pointa."""

CHRONICLE_SYSTEM = """Jsi „Ten druhý“. Napiš kroniku uplynulého měsíce uživatele: 4–8 vět, česky, věcně s lehkou
ironií, co se dělo, co zvládl, co se změnilo. Jen z podkladů."""


def _snapshot(fid: int) -> dict | None:
    f = memory.get(fid)
    return f


def _log(dream_id, user, fid, op, before, after, reason=""):
    _q("INSERT INTO fact_history(dream_id, user, fact_id, op, reason, before, after) VALUES (?, ?, ?, ?, ?, ?, ?)",
       (dream_id, user, fid, op, reason, json.dumps(before, ensure_ascii=False) if before else None,
        json.dumps(after, ensure_ascii=False) if after else None))


def _sources(ep_ids: list, episodes: dict) -> list[str]:
    return sorted({episodes[i]["conversation"] for i in ep_ids or [] if i in episodes and episodes[i]["conversation"]})


def _apply(user: str, dream_id: int, ops: dict, episodes: dict) -> dict:
    own = {f["id"]: f for f in memory.facts(user, include_family=False)}
    stats = {"add": 0, "update": 0, "reinforce": 0, "merge": 0, "archive": 0, "forget": 0}
    for a in ops.get("add") or []:
        text = (a.get("text") or "").strip()
        if not text:
            continue
        fid = memory.add_fact(user, text, kind=a.get("kind") if a.get("kind") in memory_kinds() else "fact",
                              category=a.get("category") if a.get("category") in memory.CATEGORIES else "ostatní",
                              importance=_clip(a.get("importance"), 1, 5, 3), confidence=_clip(a.get("confidence"), 0, 1, 0.7),
                              valid_until=_date(a.get("valid_until")), entities=a.get("entities") or [],
                              evidence=max(1, len(a.get("episodes") or [])), sources=_sources(a.get("episodes"), episodes))
        _log(dream_id, user, fid, "add", None, memory.get(fid))
        stats["add"] += 1
    for u in ops.get("update") or []:
        fid = u.get("id")
        if fid not in own:
            continue
        before = _snapshot(fid)
        kw = {k: u[k] for k in ("text", "category", "importance", "confidence", "valid_until", "kind") if u.get(k) is not None}
        if "valid_until" in kw:
            kw["valid_until"] = _date(kw["valid_until"])
        if u.get("reinforce"):
            kw["evidence"] = before["evidence"] + max(1, len(u.get("episodes") or []))
            kw["confidence"] = min(1.0, max(float(kw.get("confidence", before["confidence"])), before["confidence"] + 0.1))
        kw["sources"] = sorted(set(before["sources"]) | set(_sources(u.get("episodes"), episodes)))
        memory.update_fact(fid, **kw)
        _log(dream_id, user, fid, "reinforce" if u.get("reinforce") and "text" not in kw else "update", before, memory.get(fid))
        stats["reinforce" if u.get("reinforce") and "text" not in kw else "update"] += 1
    for m in ops.get("merge") or []:
        into, ids = m.get("into"), [i for i in (m.get("ids") or []) if i in own and i != m.get("into")]
        if into not in own or not ids:
            continue
        before = _snapshot(into)
        evidence = before["evidence"] + sum(own[i]["evidence"] for i in ids)
        sources = sorted(set(before["sources"]).union(*(set(own[i]["sources"]) for i in ids)))
        memory.update_fact(into, text=(m.get("text") or before["text"]).strip(), evidence=evidence, sources=sources,
                           pinned=int(before["pinned"] or any(own[i]["pinned"] for i in ids)))
        _log(dream_id, user, into, "merge", before, memory.get(into), f"sloučeno s {ids}")
        for i in ids:
            _log(dream_id, user, i, "merged_away", _snapshot(i), None, f"sloučeno do {into}")
            _q("DELETE FROM memories WHERE id = ?", (i,))
            own.pop(i, None)
        stats["merge"] += 1
    for kind in ("archive", "forget"):
        for x in ops.get(kind) or []:
            fid = x.get("id")
            if fid not in own or own[fid]["pinned"]:
                continue
            before = _snapshot(fid)
            if kind == "archive":
                memory.update_fact(fid, status="archived")
                _log(dream_id, user, fid, "archive", before, memory.get(fid), x.get("reason", ""))
            else:
                _q("DELETE FROM memories WHERE id = ?", (fid,))
                _log(dream_id, user, fid, "forget", before, None, x.get("reason", ""))
            stats[kind] += 1
    return stats


def memory_kinds():
    return ("fact", "inference", "event", "preference", "routine", "goal", "self")


def _clip(v, lo, hi, default):
    try:
        return max(lo, min(hi, type(default)(v)))
    except (TypeError, ValueError):
        return default


def _date(v) -> str | None:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat() if v else None
    except ValueError:
        return None


def _decay(user: str, dream_id: int) -> int:
    """Forgetting curve (deterministic): unreinforced, unimportant, unpinned facts fade into the archive."""
    rows = _q("SELECT id FROM memories WHERE user = ? AND status = 'active' AND pinned = 0 AND importance <= 2 "
              "AND evidence <= 1 AND kind NOT IN ('goal','self') AND last_seen < datetime('now', ?)",
              (user, f"-{DECAY_DAYS} days"))
    for (fid,) in rows:
        before = _snapshot(fid)
        memory.update_fact(fid, status="archived")
        _q("UPDATE memories SET last_seen = ? WHERE id = ?", (before["last_seen"], fid))  # keep its age
        _log(dream_id, user, fid, "decay", before, memory.get(fid), "vybledlo – dlouho nepotvrzené a nedůležité")
    return len(rows)


def _expire(user: str, dream_id: int) -> int:
    """Past events the dream left active (it often does) go to the archive."""
    rows = _q("SELECT id FROM memories WHERE user = ? AND status = 'active' AND pinned = 0 AND kind = 'event' "
              "AND valid_until IS NOT NULL AND valid_until < date('now', ?)", (user, f"-{EXPIRE_DAYS} days"))
    for (fid,) in rows:
        before = _snapshot(fid)
        memory.update_fact(fid, status="archived")
        _log(dream_id, user, fid, "archive", before, memory.get(fid), "událost proběhla")
    return len(rows)


def _feedback(user: str) -> str:
    rows = _q("""SELECT f.rating, a.content, (SELECT u.content FROM messages u WHERE u.conversation = a.conversation
                 AND u.id < a.id AND u.role = 'user' ORDER BY u.id DESC LIMIT 1)
                 FROM feedback f JOIN messages a ON a.id = f.message_id WHERE f.user = ? ORDER BY f.ts DESC LIMIT 20""", (user,))
    return "\n".join(f"{'👍' if r > 0 else '👎'} Ty: {(m or '')[:120]} | On: {a[:160]}" for r, a, m in rows) or "(žádná)"


def _rem(user: str, episodes: list[dict]) -> str:
    """The actual dream: the day's episodes mixed with images from the books."""
    from app import retrieval
    seeds = [e["text"] for e in episodes[-12:]] or [f["text"] for f in memory.facts(user)[:8]]
    excerpts = []
    for q in random.sample(seeds, min(2, len(seeds))):
        try:
            hits = retrieval.search(q, k=2)
            excerpts += [h["text"][:900] for h in hits]
        except Exception:
            pass
    ctx = ("Postřehy z jeho dne:\n" + "\n".join(f"- {s}" for s in seeds[-10:])
           + "\n\nÚryvky z knih (obrazy, atmosféra):\n" + "\n---\n".join(excerpts[:3]))
    return llm.generate(REM_SYSTEM, ctx, model=config.DREAM_MODEL, temperature=1.0, timeout_s=DREAM_TIMEOUT_S).strip()


def _chronicle(user: str):
    """On the first dream of a month, summarize the previous month."""
    today = datetime.now(TZ).date()
    prev = (today.replace(day=1) - __import__("datetime").timedelta(days=1)).replace(day=1)
    key = prev.strftime("%Y-%m")
    if _q("SELECT 1 FROM chronicle WHERE user = ? AND month = ?", (user, key)):
        return
    rows = _q("SELECT text FROM memories WHERE user = ? AND updated >= ? AND updated < ?",
              (user, prev.isoformat(), today.replace(day=1).isoformat()))
    eps = _q("SELECT text FROM episodes WHERE user = ? AND ts >= ? AND ts < ?", (user, prev.isoformat(), today.replace(day=1).isoformat()))
    if not rows and not eps:
        return
    ctx = f"Měsíc: {MONTHS[prev.month - 1]} {prev.year}\n" + "\n".join(f"- {t}" for (t,) in rows + eps)
    text = llm.generate(CHRONICLE_SYSTEM, ctx, model=config.DREAM_MODEL, timeout_s=DREAM_TIMEOUT_S).strip()
    if text:
        _q("INSERT OR REPLACE INTO chronicle(user, month, text) VALUES (?, ?, ?)", (user, key, text))


def dream(user: str) -> dict:
    """One night for one user. Returns stats; raises on total failure (logged in `dreams`)."""
    _q("SELECT 1")
    with sessions._lock:
        cur = sessions.db().execute("INSERT INTO dreams(user, model) VALUES (?, ?)", (user, config.DREAM_MODEL))
        sessions.db().commit()
        dream_id = cur.lastrowid
    try:
        eps = _q("SELECT id, ts, conversation, text, emotion FROM episodes WHERE user = ? AND dream_id IS NULL ORDER BY id", (user,))
        episodes = {i: {"id": i, "ts": t, "conversation": c, "text": x, "emotion": e} for i, t, c, x, e in eps}
        own = memory.facts(user, include_family=False)
        prof = memory.profile(user)
        rejected = [t for (t,) in _q("SELECT text FROM rejected_inferences WHERE user = ?", (user,))]
        facts_txt = "\n".join(json.dumps({k: f[k] for k in ("id", "text", "kind", "category", "importance", "confidence",
                                                               "evidence", "pinned", "valid_until", "created", "last_seen")},
                                         ensure_ascii=False) for f in own) or "(žádná)"
        eps_txt = "\n".join(f"[{e['id']}] {e['ts']} {e['text']}" + (f" ({e['emotion']})" if e["emotion"] else "")
                            for e in episodes.values()) or "(žádné)"
        loops_txt = "\n".join(f"[{lp['id']}] {lp['kind']}: {lp['text']}" + (f" (kdy: {lp['due']})" if lp["due"] else "")
                              for lp in open_items(user)) or "(žádné)"
        people_txt = "\n".join(f"- {n} ({r}): {sm}" for n, r, sm in
                               _q("SELECT name, relation, summary FROM people WHERE user = ?", (user,))) or "(nikdo)"
        ctx = (f"Aktuální datum: {now_line()}\n\nJÁDRO PROFILU:\n{prof['text'] if prof else '(zatím žádné)'}\n\n"
               f"AKTIVNÍ FAKTA:\n{facts_txt}\n\nNOVÉ POSTŘEHY:\n{eps_txt}\n\n"
               f"OTEVŘENÉ SMYČKY A OTÁZKY:\n{loops_txt}\n\nLIDÉ:\n{people_txt}\n\n"
               f"ODMÍTNUTÉ ODHADY:\n" + ("\n".join(f"- {t}" for t in rejected) or "(žádné)")
               + f"\n\nHODNOCENÍ ODPOVĚDÍ:\n{_feedback(user)}")
        system = SORT_SYSTEM.format(categories=", ".join(memory.CATEGORIES))
        ops = json.loads(llm.generate(system, ctx, model=config.DREAM_MODEL, json_mode=True, temperature=0.2,
                                      timeout_s=DREAM_TIMEOUT_S))
        stats = _apply(user, dream_id, ops, episodes)
        stats["decay"] = _decay(user, dream_id)
        stats["expire"] = _expire(user, dream_id)
        for lid in ops.get("close_loops") or []:
            _q("UPDATE open_loops SET status = 'done' WHERE id = ? AND user = ? AND status = 'open'", (lid, user))
        if (ops.get("profile") or "").strip():
            memory.set_profile(user, ops["profile"])
        for lp in ops.get("open_loops") or []:
            if (lp.get("text") or "").strip():
                _q("INSERT INTO open_loops(user, kind, text, due, dream_id) VALUES (?, 'loop', ?, ?, ?)",
                   (user, lp["text"].strip(), _date(lp.get("due")), dream_id))
        for qn in ops.get("questions") or []:
            if (qn.get("text") or "").strip():
                _q("INSERT INTO open_loops(user, kind, text, dream_id) VALUES (?, 'question', ?, ?)", (user, qn["text"].strip(), dream_id))
        for p in ops.get("people") or []:
            if (p.get("name") or "").strip():
                _q("INSERT INTO people(user, name, relation, summary) VALUES (?, ?, ?, ?) ON CONFLICT(user, name) DO UPDATE "
                   "SET relation = excluded.relation, summary = excluded.summary, updated = CURRENT_TIMESTAMP",
                   (user, p["name"].strip(), p.get("relation", ""), p.get("summary", "")))
        for name in ops.get("remove_people") or []:
            _q("DELETE FROM people WHERE user = ? AND name = ?", (user, str(name).strip()))
        if (ops.get("style_notes") or "").strip():
            _q("INSERT INTO style_notes(user, text) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET text = excluded.text, "
               "updated = CURRENT_TIMESTAMP", (user, ops["style_notes"].strip()))
        if episodes:
            _q(f"UPDATE episodes SET dream_id = ? WHERE id IN ({','.join('?' * len(episodes))})", (dream_id, *episodes))
        # Old open loops nobody followed up fade after their due date + 3 days.
        _q("UPDATE open_loops SET status = 'expired' WHERE user = ? AND status = 'open' AND due IS NOT NULL "
           "AND due < date('now', '-3 days')", (user,))
        # Never more than MAX_LOOPS waiting: the soonest stay.
        _q("UPDATE open_loops SET status = 'expired' WHERE user = ? AND kind = 'loop' AND status = 'open' AND id NOT IN "
           "(SELECT id FROM open_loops WHERE user = ? AND kind = 'loop' AND status = 'open' "
           "ORDER BY COALESCE(due, '9999'), id LIMIT ?)", (user, user, MAX_LOOPS))
        memory.embed_missing(user)
        try:
            dream_text = _rem(user, list(episodes.values()))
        except Exception as e:
            dream_text = ""
            print(f"  [dream] REM {user}: {str(e)[:100]}", flush=True)
        try:
            if datetime.now(TZ).day <= 7:
                _chronicle(user)
        except Exception as e:
            print(f"  [dream] chronicle {user}: {str(e)[:100]}", flush=True)
        stats["episodes"] = len(episodes)
        _q("UPDATE dreams SET stats = ?, dream_text = ? WHERE id = ?", (json.dumps(stats), dream_text, dream_id))
        print(f"  [dream] {user}: {stats}", flush=True)
        return {"dream_id": dream_id, **stats, "dream_text": dream_text}
    except Exception as e:
        _q("UPDATE dreams SET error = ? WHERE id = ?", (str(e)[:500], dream_id))
        raise


# ---- undo ----

def undo(user: str, history_id: int) -> bool:
    rows = _q("SELECT fact_id, op, before, after, undone FROM fact_history WHERE id = ? AND user = ?", (history_id, user))
    if not rows or rows[0][4]:
        return False
    fid, op, before, after, _ = rows[0]
    before = json.loads(before) if before else None
    if op == "add":
        _q("DELETE FROM memories WHERE id = ?", (fid,))
    elif before:
        if memory.get(fid) is None:  # forgotten / merged away → put it back with the same id
            memory._db()
            cols = [c for c in memory.FACT_FIELDS if c not in ("created", "updated")]
            vals = [json.dumps(before[c], ensure_ascii=False) if c in ("sources", "entities") else before[c] for c in cols]
            _q(f"INSERT INTO memories({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(vals))
        else:
            memory.update_fact(fid, **{k: before[k] for k in ("text", "kind", "category", "confidence", "evidence",
                                                               "importance", "pinned", "status", "valid_until",
                                                               "sources", "entities")})
    if op == "reject" and before:  # un-rejecting: the dream may infer it again
        _q("DELETE FROM rejected_inferences WHERE user = ? AND text = ?", (user, before["text"]))
    _q("UPDATE fact_history SET undone = 1 WHERE id = ?", (history_id,))
    return True


# ---- data for prompts / brief / UI ----

def last_dream(user: str) -> dict | None:
    rows = _q("SELECT id, ts, dream_text, stats FROM dreams WHERE user = ? AND error IS NULL ORDER BY id DESC LIMIT 1", (user,))
    if not rows:
        return None
    i, ts, txt, st = rows[0]
    return {"id": i, "ts": ts, "dream_text": txt, "stats": json.loads(st or "{}")}


def open_items(user: str, kind: str | None = None) -> list[dict]:
    rows = _q("SELECT id, kind, text, due, created FROM open_loops WHERE user = ? AND status = 'open'"
              + (" AND kind = ?" if kind else "") + " ORDER BY COALESCE(due, '9999'), id", (user, kind) if kind else (user,))
    return [{"id": i, "kind": k, "text": t, "due": d, "created": c} for i, k, t, d, c in rows]


def style(user: str) -> str:
    rows = _q("SELECT text FROM style_notes WHERE user = ?", (user,))
    return rows[0][0] if rows else ""


def dreams_in_brief(user: str) -> bool:
    rows = _q("SELECT dreams_in_brief FROM users WHERE user = ?", (user,))
    return bool(rows[0][0]) if rows else True


def set_dreams_in_brief(user: str, on: bool):
    _q("INSERT INTO users(user, dreams_in_brief) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET dreams_in_brief = "
       "excluded.dreams_in_brief", (user, int(on)))


def users_to_dream() -> list[str]:
    rows = _q("SELECT DISTINCT user FROM episodes WHERE dream_id IS NULL UNION SELECT DISTINCT user FROM memories")
    return [u for (u,) in rows]


def tick():
    """Scheduler hook: dream once per user per night, from DREAM_AT."""
    now = datetime.now(TZ)
    if not ((now.hour, now.minute) >= DREAM_AT and now.hour < 9):
        return
    for user in users_to_dream():
        done = _q("SELECT 1 FROM dreams WHERE user = ? AND error IS NULL AND date(ts, 'localtime') = date('now', 'localtime')", (user,))
        tried = _q("SELECT COUNT(*) FROM dreams WHERE user = ? AND date(ts, 'localtime') = date('now', 'localtime')", (user,))[0][0]
        if done or tried >= 3:
            continue
        try:
            dream(user)
        except Exception as e:
            print(f"  [dream] {user}: {str(e)[:150]}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    who = args[args.index("--user") + 1] if "--user" in args else "local"
    t = time.time()
    print(json.dumps(dream(who), ensure_ascii=False, indent=1), f"\n{time.time() - t:.0f}s")

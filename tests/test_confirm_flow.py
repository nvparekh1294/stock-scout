"""scout/test_confirm_flow.py — unit cases for the underwrite confirm-flow fix.

Plain-Python asserts (no pytest, no LLM spend, no Telegram network, no real
underwrites — llm.call / dispatch / run_underwrite_sync / resolve_turn are
stubbed, and Telegram Update/Query objects are faked):

    scout/.venv/bin/python -m scout.test_confirm_flow

Covers the four tasks of the confirm-flow fix:
  T2  transport guard: a queue-PROMISE reply with nothing staged never ships —
      it triggers one corrective iteration, then a real button or a safe
      fallback (never the false promise). Ordinary cost talk is never blocked.
  T3  deterministic yes/no while an underwrite is pending: a bare affirmative
      executes exactly what tapping Confirm does (SHARED helper confirm_and_run,
      asserted identical for the button and the text path); a bare negative
      cancels; anything else routes to the agent with pending left intact.
  T4  tool-call observability: one safe stdout line per tool call (whitelisted
      params only — never free text or secrets) + a line when the guard fires.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile

from scout import telegram_bot as t


class _FP:
    """Profile stub for the async on_text tests: the owner is already onboarded,
    so the first-run intake gate passes straight through to the confirm flow
    (no interview). Patched onto telegram_bot.profile via _patched(profile=_FP)."""
    @staticmethod
    def wants_redo(text):
        return False

    @staticmethod
    def is_configured(db, chat_id):
        return True


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeChat:
    def __init__(self, cid):
        self.id = cid
        self.actions = []
        self.docs = []

    async def send_action(self, a):
        self.actions.append(a)

    async def send_document(self, fh, filename=None, caption=None):
        self.docs.append({"filename": filename, "caption": caption})


class FakeMessage:
    def __init__(self, text, chat):
        self.text = text
        self.chat = chat
        self.replies = []
        self.markups = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data, chat, message):
        self.data = data
        self.message = message
        self.edits = []
        self.answered = False

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text):
        self.edits.append(text)


class FakeUpdate:
    def __init__(self, chat, message=None, cq=None):
        self.effective_chat = chat
        self.message = message
        self.callback_query = cq


@contextlib.contextmanager
def _patched(**overrides):
    """Temporarily set telegram_bot module globals; always restore."""
    saved = {k: getattr(t, k) for k in overrides}
    for k, v in overrides.items():
        setattr(t, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(t, k, v)


def _tmp_html() -> str:
    fh = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
    fh.write(b"<html>brief</html>")
    fh.close()
    return fh.name


def _make_turn(seq):
    """Stub agent_turn: returns seq[n] on the nth call, records corrective."""
    calls = []

    def fn(chat_id, user_text, corrective=None):
        calls.append({"user_text": user_text, "corrective": corrective})
        return seq[len(calls) - 1]

    fn.calls = calls
    return fn


# ── T3: classify_pending_reply ───────────────────────────────────────────────
def test_classify_affirmatives():
    for w in ("yes", "Yes", "YES", "y", "yes!", "confirm", "go", "go ahead",
              "do it", "run it", "yep", "yeah", "sure", "ok", "okay", "ok.", "go!"):
        assert t.classify_pending_reply(w) == "confirm", w


def test_classify_negatives():
    for w in ("no", "No", "cancel", "stop", "nevermind", "never mind", "don't",
              "nah", "no."):
        assert t.classify_pending_reply(w) == "cancel", w


def test_classify_non_bare_returns_none():
    # sentences, questions, other topics → None (routes to the agent, pending stays)
    for w in ("yes please run the full one", "what's the thesis on OPTC?",
              "why does it cost that much", "go long on HLXR", "", "   ",
              "actually no let's do QMEM instead"):
        assert t.classify_pending_reply(w) is None, w


# ── T3: route_incoming (pure decision, no mutation) ──────────────────────────
def test_route_incoming_no_pending_is_agent():
    with _patched(PENDING={}):
        assert t.route_incoming(1, "yes") == "agent"        # nothing staged
        assert t.route_incoming(1, "hello") == "agent"


def test_route_incoming_pending_affirmative_confirm():
    with _patched(PENDING={7: {"symbol": "HLXR", "depth": "standard", "est": "0.4"}}):
        assert t.route_incoming(7, "yes") == "confirm"
        assert t.PENDING[7]["symbol"] == "HLXR"             # not mutated


def test_route_incoming_pending_negative_cancel():
    with _patched(PENDING={7: {"symbol": "HLXR", "depth": "standard", "est": "0.4"}}):
        assert t.route_incoming(7, "cancel") == "cancel"


def test_route_incoming_pending_unrelated_agent():
    with _patched(PENDING={7: {"symbol": "HLXR", "depth": "standard", "est": "0.4"}}):
        assert t.route_incoming(7, "what's the downside case?") == "agent"


# ── T2: _is_queue_promise ────────────────────────────────────────────────────
def test_is_queue_promise_positive():
    for s in ("Queued — HLXR standard. Won't spend until you tap Confirm.",
              "I've set up a confirm button for you.",
              "Just tap Confirm below.",
              "Queued the underwrite.",
              "There's a confirm button waiting.",
              "Won’t spend until you tap it."):  # curly apostrophe
        assert t._is_queue_promise(s), s


def test_is_queue_promise_ignores_cost_talk():
    for s in ("A standard underwrite of HLXR costs about $0.50 — want it?",
              "That would run roughly $0.40 to $0.70.",
              "The quick take is ~$0.20, not free.",
              "Here's the thesis on OPTC.", ""):
        assert not t._is_queue_promise(s), s


# ── T4: _tool_log_line ───────────────────────────────────────────────────────
def test_tool_log_line_safe_params_only():
    line = t._tool_log_line("run_underwrite",
                            {"symbol": "HLXR", "depth": "standard",
                             "note": "SECRET_private_thesis_text"})
    assert line.startswith("[tool] run_underwrite"), line
    assert "symbol=HLXR" in line and "depth=standard" in line, line
    # free-text / non-whitelisted params must never appear
    assert "SECRET" not in line and "note" not in line, line


def test_tool_log_line_truncates_and_handles_empty():
    long = "A" * 100
    line = t._tool_log_line("resolve_ticker", {"query": long})
    assert "…" in line and long not in line, line
    assert t._tool_log_line("run_scorecard", {}).endswith("(no safe params)")


# ── T2: resolve_turn guard paths ─────────────────────────────────────────────
def test_resolve_turn_normal_reply_untouched():
    # normal text + no pending → sent as-is, guard never fires, single turn.
    turn = _make_turn([("A standard underwrite costs ~$0.50, want it?", None, [])])
    reply, pending, docs, fired = t.resolve_turn(1, "how much for a standard?", turn)
    assert reply == "A standard underwrite costs ~$0.50, want it?"
    assert pending is None and fired is False and len(turn.calls) == 1


def test_resolve_turn_pending_set_is_untouched():
    # tool WAS called (pending set) even though the wording mentions a button →
    # guard is skipped; the button path stays exactly as today.
    pend = {"symbol": "HLXR", "depth": "standard", "est": "0.40"}
    turn = _make_turn([("I've queued it — tap Confirm below.", pend, [])])
    reply, pending, docs, fired = t.resolve_turn(1, "run standard on HLXR", turn)
    assert pending is pend and fired is False and len(turn.calls) == 1


def test_resolve_turn_promise_no_pending_retry_sets_pending():
    # the bug: promise text, nothing staged → one corrective iteration that this
    # time makes a real tool call → the real button is what ships.
    pend = {"symbol": "HLXR", "depth": "standard", "est": "0.40"}
    turn = _make_turn([
        ("Queued — HLXR. Won't spend until you tap Confirm.", None, []),
        ("A standard underwrite of HLXR costs ~$0.40.", pend, [])])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply, pending, docs, fired = t.resolve_turn(
            1, "run the standard underwrite on HLXR", turn)
    assert pending is pend and fired is True, (pending, fired)
    assert len(turn.calls) == 2 and turn.calls[1]["corrective"], turn.calls
    assert "[guard]" in buf.getvalue()                      # guard-fired log line


def test_resolve_turn_promise_no_pending_retry_fails_fallback():
    # retry STILL narrates instead of calling the tool → safe fallback, no
    # pending, and the false promise is never returned.
    turn = _make_turn([
        ("Queued — HLXR. Won't spend until you tap Confirm.", None, []),
        ("Sure, tap Confirm whenever you're ready.", None, [])])
    reply, pending, docs, fired = t.resolve_turn(
        1, "please run the standard underwrite on HLXR", turn)
    assert pending is None and fired is True
    assert "didn't stage it properly" in reply, reply
    assert "HLXR" in reply, reply                           # symbol recovered
    assert not t._is_queue_promise(reply), reply            # never a false promise


# ── T3: confirm_and_run is the shared execution helper ───────────────────────
def test_confirm_and_run_runs_and_persists():
    saved = []
    with _patched(run_underwrite_sync=lambda s, d: (f"{s} {d} done", "/tmp/x.html"),
                  save_message=lambda cid, role, content: saved.append((role, content))):
        summary, html = t.confirm_and_run(7, {"symbol": "HLXR", "depth": "standard"})
    assert summary == "HLXR standard done" and html == "/tmp/x.html"
    assert saved[0][0] == "user" and "confirmed" in saved[0][1]
    assert saved[1] == ("assistant", "HLXR standard done")


# ── async handler wiring (T3 identity + T2 delivery) ─────────────────────────
def test_on_text_and_on_callback_confirm_are_identical():
    """The text 'yes' and the Confirm tap call the SAME helper with the SAME
    args and both deliver the summary + brief document."""
    html = _tmp_html()
    pend = {"symbol": "HLXR", "depth": "standard", "est": "0.40"}
    seen = []

    def fake_confirm(chat_id, pending):
        seen.append((chat_id, dict(pending)))
        return ("HLXR standard done · verdict PASS", html)

    # --- text 'yes' path ---
    with _patched(OWNER_ID="777", profile=_FP, PENDING={777: dict(pend)},
                  confirm_and_run=fake_confirm):
        chat = FakeChat(777)
        msg = FakeMessage("yes", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert any("HLXR standard done" in r for r in msg.replies), msg.replies
        assert chat.docs and chat.docs[0]["caption"].startswith("Full brief")
        assert 777 not in t.PENDING                         # popped
        text_reply, text_docs = list(msg.replies), list(chat.docs)

    # --- Confirm-button path ---
    with _patched(OWNER_ID="777", profile=_FP, PENDING={777: dict(pend)},
                  confirm_and_run=fake_confirm):
        chat2 = FakeChat(777)
        msg2 = FakeMessage("", chat2)
        q = FakeQuery("uw_confirm", chat2, msg2)
        asyncio.run(t.on_callback(FakeUpdate(chat2, cq=q), None))
        assert any("HLXR standard done" in r for r in msg2.replies), msg2.replies
        assert chat2.docs and chat2.docs[0]["caption"].startswith("Full brief")
        assert 777 not in t.PENDING

    # identical helper call from both entry points
    assert seen[0] == seen[1] == (777, pend), seen


def test_on_text_negative_cancels_without_running():
    ran = []
    with _patched(OWNER_ID="777", profile=_FP, PENDING={777: {"symbol": "HLXR", "depth": "standard", "est": "0.4"}},
                  save_message=lambda *a: None,   # Finding D: cancel path now persists — stub to stay hermetic
                  confirm_and_run=lambda *a: ran.append(a) or ("x", None)):
        chat = FakeChat(777)
        msg = FakeMessage("cancel", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert msg.replies == [t.CANCEL_TEXT], msg.replies
        assert 777 not in t.PENDING and ran == []           # nothing executed


def test_on_text_unrelated_message_leaves_pending_and_hits_agent():
    hit = []
    with _patched(OWNER_ID="777", profile=_FP, PENDING={777: {"symbol": "HLXR", "depth": "standard", "est": "0.4"}},
                  save_message=lambda *a: None,
                  resolve_turn=lambda cid, txt: (hit.append(txt) or "here's the downside", None, [], False)):
        chat = FakeChat(777)
        msg = FakeMessage("what's the downside case?", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert hit == ["what's the downside case?"]         # went to the agent
        assert 777 in t.PENDING                             # pending left intact
        assert msg.markups[-1] is None                      # no new button


def test_on_text_affirmative_with_no_pending_goes_to_agent():
    hit = []
    with _patched(OWNER_ID="777", profile=_FP, PENDING={},
                  save_message=lambda *a: None,
                  resolve_turn=lambda cid, txt: (hit.append(txt) or "sure, here's a thought", None, [], False)):
        chat = FakeChat(777)
        msg = FakeMessage("yes", chat)                      # bare yes but nothing staged
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert hit == ["yes"]                               # agent, not a confirm


def test_on_text_pending_from_agent_attaches_button():
    pend = {"symbol": "HLXR", "depth": "standard", "est": "0.40"}
    with _patched(OWNER_ID="777", profile=_FP, PENDING={},
                  save_message=lambda *a: None,
                  resolve_turn=lambda cid, txt: ("A standard underwrite of HLXR costs ~$0.40.", pend, [], False)):
        chat = FakeChat(777)
        msg = FakeMessage("run standard on HLXR", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert t.PENDING[777] == pend                       # staged for the button
        assert msg.markups[-1] is not None                  # InlineKeyboard attached


def test_on_text_guard_fallback_sends_no_button():
    fallback = t._fallback_text("HLXR")
    with _patched(OWNER_ID="777", profile=_FP, PENDING={},
                  save_message=lambda *a: None,
                  resolve_turn=lambda cid, txt: (fallback, None, [], True)):
        chat = FakeChat(777)
        msg = FakeMessage("run standard on HLXR", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert any("didn't stage it properly" in r for r in msg.replies), msg.replies
        assert msg.markups[-1] is None                      # never a button w/o a live one
        assert 777 not in t.PENDING


# ── T4: agent_turn emits a safe tool-call log line ───────────────────────────
def test_agent_turn_logs_tool_calls():
    seq = [
        {"stop_reason": "tool_use", "raw_content": [],
         "tool_uses": [{"id": "1", "name": "run_underwrite",
                        "input": {"symbol": "HLXR", "depth": "standard",
                                  "note": "SECRET_thesis"}}]},
        {"stop_reason": "end_turn", "text": "done here", "tool_uses": []},
    ]

    def fake_call(*a, **k):
        r = seq[fake_call.i]
        fake_call.i += 1
        return r
    fake_call.i = 0

    class _LLM:
        call = staticmethod(fake_call)

    with _patched(llm=_LLM, dispatch=lambda name, ti, ctx: "ok",
                  load_history=lambda cid: []):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reply, pending, docs = t.agent_turn(7, "run standard on HLXR")
    out = buf.getvalue()
    assert reply == "done here", reply
    assert "[tool] run_underwrite" in out, out
    assert "symbol=HLXR" in out and "depth=standard" in out, out
    assert "SECRET" not in out, out                         # no free text / secrets


# ── Finding A: stale-pending TTL on the text yes/no short-circuit ─────────────
def test_route_incoming_fresh_pending_affirmative_confirms():
    now = t.time.time()
    with _patched(PENDING={7: {"symbol": "HLXR", "depth": "standard",
                               "est": "0.4", "staged_at": now}}):
        assert t.route_incoming(7, "ok") == "confirm"
        assert 7 in t.PENDING                                # not mutated


def test_route_incoming_stale_pending_affirmative_goes_to_agent():
    # a bare "ok" typed long after staging must NOT fire the paid underwrite.
    old = t.time.time() - (t.PENDING_TEXT_CONFIRM_TTL_SECONDS + 60)
    with _patched(PENDING={7: {"symbol": "HLXR", "depth": "standard",
                               "est": "0.4", "staged_at": old}}):
        assert t.route_incoming(7, "ok") == "agent"          # routed to the agent
        assert t.PENDING[7]["symbol"] == "HLXR"              # pending left intact


def test_route_incoming_stale_pending_negative_goes_to_agent():
    old = t.time.time() - (t.PENDING_TEXT_CONFIRM_TTL_SECONDS + 60)
    with _patched(PENDING={7: {"symbol": "HLXR", "depth": "standard",
                               "est": "0.4", "staged_at": old}}):
        assert t.route_incoming(7, "no") == "agent"
        assert 7 in t.PENDING


def test_stale_pending_button_still_confirms():
    # the BUTTON path ignores age entirely — on_callback runs regardless of when
    # it was staged (button semantics unchanged by Finding A).
    old = t.time.time() - (t.PENDING_TEXT_CONFIRM_TTL_SECONDS + 3600)
    seen = []

    def fake_confirm(chat_id, pending):
        seen.append((chat_id, dict(pending)))
        return ("HLXR standard done", None)

    with _patched(OWNER_ID="777", profile=_FP,
                  PENDING={777: {"symbol": "HLXR", "depth": "standard",
                                 "est": "0.4", "staged_at": old}},
                  confirm_and_run=fake_confirm):
        chat = FakeChat(777)
        msg = FakeMessage("", chat)
        q = FakeQuery("uw_confirm", chat, msg)
        asyncio.run(t.on_callback(FakeUpdate(chat, cq=q), None))
        assert seen and seen[0][0] == 777                    # confirmed despite age
        assert 777 not in t.PENDING


def test_on_text_stale_pending_affirmative_hits_agent():
    # end-to-end via on_text: stale pending + "ok" → agent, pending intact, no run.
    old = t.time.time() - (t.PENDING_TEXT_CONFIRM_TTL_SECONDS + 60)
    hit, ran = [], []
    with _patched(OWNER_ID="777", profile=_FP,
                  PENDING={777: {"symbol": "HLXR", "depth": "standard",
                                 "est": "0.4", "staged_at": old}},
                  save_message=lambda *a: None,
                  confirm_and_run=lambda *a: ran.append(a) or ("x", None),
                  resolve_turn=lambda cid, txt: (hit.append(txt) or "sure thing", None, [], False)):
        chat = FakeChat(777)
        msg = FakeMessage("ok", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert hit == ["ok"] and ran == []                   # agent ran, no underwrite
        assert 777 in t.PENDING                              # pending left intact


# ── Finding B: guard gated on current-turn underwrite intent ──────────────────
def test_shows_underwrite_intent():
    for yes in ("run the standard underwrite on HLXR", "standard brief on X",
                "give me the full deep dive", "queue it", "ok", "yes", "do it"):
        assert t._shows_underwrite_intent(yes), yes
    for no in ("what does the confirm button do?", "how are you today?",
               "what's the downside case?", "", "tell me about the company"):
        assert not t._shows_underwrite_intent(no), no


def test_resolve_turn_help_question_promise_untouched():
    # Finding B: a promise-y EXPLAINER reply to a meta/help question with nothing
    # staged is sent as-is — guard never fires, no corrective iteration, no log.
    promise = "The Confirm button appears when you queue an underwrite."
    turn = _make_turn([(promise, None, [])])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply, pending, docs, fired = t.resolve_turn(
            1, "what does the confirm button do?", turn)
    assert reply == promise and pending is None and fired is False
    assert len(turn.calls) == 1                              # no corrective retry
    assert "[guard]" not in buf.getvalue()                  # no guard log line


def test_resolve_turn_real_intent_promise_still_guarded():
    # a real underwrite ask ("standard brief on X") + promise text + no pending →
    # guard fires exactly as before Finding B.
    pend = {"symbol": "HLXR", "depth": "standard", "est": "0.40"}
    turn = _make_turn([
        ("Queued — HLXR. Won't spend until you tap Confirm.", None, []),
        ("A standard underwrite of HLXR costs ~$0.40.", pend, [])])
    reply, pending, docs, fired = t.resolve_turn(1, "standard brief on HLXR", turn)
    assert pending is pend and fired is True
    assert len(turn.calls) == 2 and turn.calls[1]["corrective"]


# ── Finding C: curly-apostrophe negatives ─────────────────────────────────────
def test_classify_curly_apostrophe_negative():
    assert t.classify_pending_reply("don’t") == "cancel"   # iOS curly "don’t"
    assert t.classify_pending_reply("Don’t.") == "cancel"


# ── Finding D: both cancel paths persist an identical decline pair ────────────
def test_cancel_paths_persist_identical_rows():
    pend = {"symbol": "HLXR", "depth": "standard", "est": "0.4"}

    # text-cancel path
    text_saved = []
    with _patched(OWNER_ID="777", profile=_FP, PENDING={777: dict(pend)},
                  save_message=lambda cid, role, content: text_saved.append((cid, role, content))):
        chat = FakeChat(777)
        msg = FakeMessage("cancel", chat)
        asyncio.run(t.on_text(FakeUpdate(chat, message=msg), None))
        assert msg.replies == [t.CANCEL_TEXT]
        assert 777 not in t.PENDING

    # button-cancel path
    btn_saved = []
    with _patched(OWNER_ID="777", profile=_FP, PENDING={777: dict(pend)},
                  save_message=lambda cid, role, content: btn_saved.append((cid, role, content))):
        chat2 = FakeChat(777)
        msg2 = FakeMessage("", chat2)
        q = FakeQuery("uw_cancel", chat2, msg2)
        asyncio.run(t.on_callback(FakeUpdate(chat2, cq=q), None))
        assert q.edits == [t.CANCEL_TEXT]
        assert 777 not in t.PENDING

    # identical rows written from both entry points
    assert text_saved == btn_saved, (text_saved, btn_saved)
    assert text_saved[0] == (777, "user", "[cancelled underwrite HLXR]")
    assert text_saved[1] == (777, "assistant", t.CANCEL_TEXT)


def test_persist_cancel_without_pending_uses_generic_marker():
    saved = []
    with _patched(save_message=lambda cid, role, content: saved.append((role, content))):
        t.persist_cancel(9, None)
    assert saved[0] == ("user", "[cancelled the pending underwrite]")
    assert saved[1] == ("assistant", t.CANCEL_TEXT)

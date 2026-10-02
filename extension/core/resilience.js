// SPDX-License-Identifier: GPL-3.0-or-later
// core/resilience.js - provider-agnostic ERROR-RECOVERY policy for the loop.
//
// WHY THIS EXISTS
// The agentic loop is meant to be left alone. Historically a single hiccup - the
// site dropping a reply, a 5-minute inactivity timeout, a swallowed send, a
// half-written command - ended the loop with a banner and a greyed "not run"
// chip, and the user had to notice and re-prompt by hand. That is fine for an
// attended session and useless for an unattended one.
//
// This module is the DECISION layer: given what just went wrong and how many
// times it has already gone wrong, it says whether to retry (and after how
// long), to keep waiting, or to give up honestly. It never decides WHAT to send
// - the caller supplies the feedback text - so correctness is untouched: a
// retry can only ever re-ask, never fake a result.
//
// It is deliberately PURE (no DOM, no timers, no chrome.*) so the whole policy
// is unit-testable in plain node (see tests/test_error_recovery.js).
// eslint-disable-next-line no-unused-vars
const ZSResilience = (() => {
  "use strict";

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const num = (v, f) => { const n = Number(v); return Number.isFinite(n) ? n : f; };

  // ── Levels ────────────────────────────────────────────────────────────────
  // maxRetries      : per-failure retry budget (resets when a turn succeeds).
  // maxConsecutive  : give up once this many failures happen back-to-back, so a
  //                   genuinely broken session stops instead of burning quota.
  // backoffMs       : delay before retry N (last value repeats).
  const LEVELS = {
    off: {
      label: "Off", hint: "A failure ends the loop, as before.",
      maxRetries: 0, maxConsecutive: 0, backoffMs: [],
      retrySilence: false, retryParse: false, retrySend: false, autoContinue: false,
    },
    standard: {
      label: "Standard", hint: "Recovers from the common one-off hiccups.",
      maxRetries: 2, maxConsecutive: 3, backoffMs: [2500, 6000],
      retrySilence: true, retryParse: true, retrySend: true, autoContinue: false,
    },
    persistent: {
      label: "Persistent", hint: "Keeps a long unattended run alive through repeated stalls.",
      maxRetries: 6, maxConsecutive: 8,
      backoffMs: [3000, 8000, 15000, 25000, 40000, 60000],
      retrySilence: true, retryParse: true, retrySend: true, autoContinue: true,
    },
  };
  const LEVEL_IDS = Object.keys(LEVELS);

  const DEFAULT_SETTINGS = { level: "standard", perProvider: {} };

  function sanitize(raw) {
    const src = raw && typeof raw === "object" ? raw : {};
    const out = { level: LEVEL_IDS.includes(src.level) ? src.level : DEFAULT_SETTINGS.level, perProvider: {} };
    if (src.perProvider && typeof src.perProvider === "object") {
      for (const [id, v] of Object.entries(src.perProvider)) {
        if (!/^[a-z0-9_-]{1,40}$/i.test(id)) continue;
        if (LEVEL_IDS.includes(v)) out.perProvider[id] = v;
      }
    }
    return out;
  }

  function levelFor(settings, providerId) {
    const s = settings && typeof settings === "object" ? settings : DEFAULT_SETTINGS;
    const per = s.perProvider && providerId ? s.perProvider[providerId] : null;
    if (per && LEVEL_IDS.includes(per)) return per;
    return LEVEL_IDS.includes(s.level) ? s.level : DEFAULT_SETTINGS.level;
  }
  const policy = (level) => LEVELS[level] || LEVELS.standard;

  // ── Feedback text ─────────────────────────────────────────────────────────
  // Injected as a normal (camouflaged) turn. Every one of them is explicit that
  // nothing was run, so a model can never read a recovery nudge as a result.
  const FEEDBACK = {
    silence: (why) =>
      "(System note: your previous turn produced no readable text - " + why + ". " +
      "Nothing from it was executed. Do not apologise, do not repeat earlier context, and do not " +
      "restate the plan. Continue the task from where you stopped: if a command was still pending, " +
      "write it again as exactly ONE plain-text JSON object; otherwise continue the work or give the " +
      "final answer.)",
    parse: (name) =>
      "(System note: your previous reply looked like a Multi-Script command but was not one complete " +
      "plain-text JSON object, so it was NOT executed. " +
      (name && name !== "command" ? `The command name detected was "${name}". ` : "") +
      'Write it again as exactly {"command": "exact_name", "params": { ...all parameters... }} - ' +
      "no code fences, no XML-like tags, no special markers, one command only.)",
    send: () =>
      "(System note: the extension could not hand its previous message to the composer, so the model " +
      "never received it. This is a local hiccup, not a mistake by the model. The message is being " +
      "sent again now.)",
    busy: () =>
      "(System note: the site itself reported a transient problem - it was busy or asked to try again - " +
      "so your previous turn was not a real answer and NOTHING from it was executed. The site is being " +
      "retried now. Do not apologise and do not explain the outage. Continue the task from where you " +
      "stopped: if a command was still pending, write it again as exactly ONE plain-text JSON object; " +
      "otherwise continue the work or give the final answer.)",
  };

  // ── The decision ──────────────────────────────────────────────────────────
  // kind: "empty" | "timeout" | "parse_error" | "send_failed" | "verification"
  //       | "bridge_offline" | "terminal"
  // ctx : { attempt, consecutive, feedback, name }
  //   attempt     - retries ALREADY spent on this same failure (0 on the first).
  //   consecutive - failures in a row, across kinds (resets on any success).
  //   feedback    - the caller's text (e.g. ZS.FEEDBACK.parseError); optional.
  // Returns { action, delayMs, feedback, reason, attemptsLeft, level }
  //   action: "retry"  → send `feedback` as a new turn after delayMs
  //           "resend" → send the SAME outbound text again after delayMs
  //           "wait"   → park and re-check (verification / bridge); no send
  //           "stop"   → end the loop and report honestly
  function decide(level, kind, ctx = {}) {
    const L = policy(level);
    const attempt = Math.max(0, Math.round(num(ctx.attempt, 0)));
    const consecutive = Math.max(0, Math.round(num(ctx.consecutive, 0)));
    const delayFor = (i) => (L.backoffMs.length ? L.backoffMs[Math.min(i, L.backoffMs.length - 1)] : 0);
    const out = (action, delayMs, feedback, reason) => ({
      action, delayMs, feedback: feedback || "", reason, level,
      attemptsLeft: Math.max(0, L.maxRetries - attempt),
    });
    const budgetSpent = () => attempt >= L.maxRetries;
    const tooMany = () => L.maxConsecutive > 0 && consecutive >= L.maxConsecutive;

    switch (kind) {
      case "verification":
        // Never a retry: the user must clear it. The loop parks and re-checks,
        // which is exactly what "wait" means here.
        return out("wait", 0, "", "human-verification");
      case "bridge_offline":
        // The bridge or the editor is down; it is not the model's fault and
        // re-prompting cannot help. Park and re-check.
        return out("wait", 0, "", "environment-offline");
      case "terminal":
        return out("stop", 0, "", "terminal");
      case "empty":
      case "timeout": {
        if (!L.retrySilence) return out("stop", 0, "", "recovery-off");
        if (budgetSpent()) return out("stop", 0, "", "silence-retry-budget-spent");
        if (tooMany()) return out("stop", 0, "", "too-many-consecutive-failures");
        const why = kind === "timeout"
          ? "the site stopped responding in time"
          : "the reply was empty or the site dropped it";
        return out("retry", delayFor(attempt), FEEDBACK.silence(why), kind);
      }
      case "busy": {
        // The SITE said it was busy. Worth exactly ONE retry: if it is still busy
        // a moment later, waiting longer is the user's call, not ours. The single
        // attempt also caps the cost of the rare false positive where the MODEL's
        // own short answer contains the site's phrasing ("...please try again").
        if (!L.retrySilence) return out("stop", 0, "", "recovery-off");
        if (attempt >= 1) return out("stop", 0, "", "busy-retry-spent");
        if (tooMany()) return out("stop", 0, "", "too-many-consecutive-failures");
        return out("retry", delayFor(attempt), FEEDBACK.busy(), "busy");
      }
      case "parse_error": {
        if (!L.retryParse) return out("stop", 0, "", "recovery-off");
        if (budgetSpent()) return out("stop", 0, "", "parse-retry-budget-spent");
        if (tooMany()) return out("stop", 0, "", "too-many-consecutive-failures");
        return out("retry", delayFor(attempt), ctx.feedback || FEEDBACK.parse(ctx.name), "parse_error");
      }
      case "send_failed": {
        if (!L.retrySend) return out("stop", 0, "", "recovery-off");
        if (budgetSpent()) return out("stop", 0, "", "send-retry-budget-spent");
        if (tooMany()) return out("stop", 0, "", "too-many-consecutive-failures");
        return out("resend", delayFor(attempt), ctx.feedback || "", "send_failed");
      }
      default:
        return out("stop", 0, "", "unknown");
    }
  }

  // Budgets the caller uses to widen its own timers. A persistent run gets a
  // longer patience for a slow reply and for a still-open command block.
  function budgets(level) {
    const L = policy(level);
    const persistent = L === LEVELS.persistent;
    return {
      level,
      // Extra patience folded into waitForResponse's pre-start silent window.
      preStartSilentMs: persistent ? 120000 : 60000,
      // Extra patience before a warm, content-free turn is declared empty.
      warmupMs: persistent ? 75000 : 45000,
      // Extra patience for a reasoning phase with no answer yet.
      reasonNoReplyMs: persistent ? 150000 : 90000,
      // Extra patience for an open (still-streaming) command block.
      openBlockGraceMs: persistent ? 12000 : 6000,
      // How long to keep waiting for the user to clear a bot-check.
      verificationWaitMs: persistent ? 900000 : 600000,
      autoContinue: !!L.autoContinue,
    };
  }

  function describe(settings, providerId) {
    const level = levelFor(settings, providerId);
    const L = policy(level);
    if (L.maxRetries === 0) return "Recovery off · a failure ends the run";
    return `${L.label} · up to ${L.maxRetries} retries per failure · gives up after ${L.maxConsecutive} failures in a row`;
  }

  return {
    LEVELS, LEVEL_IDS, DEFAULT_SETTINGS, FEEDBACK,
    sanitize, levelFor, policy, decide, budgets, describe,
  };
})();

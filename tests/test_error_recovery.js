// SPDX-License-Identifier: GPL-3.0-or-later
// Error-recovery policy regression. Pure logic (core/resilience.js) driven
// directly: this is the gate that decides whether an unattended run keeps going
// through a stall or stops honestly, so every branch is asserted explicitly.
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..');
const ZSResilience = new Function(fs.readFileSync(path.join(root, 'extension/core/resilience.js'), 'utf8') + ';return ZSResilience;')();
const ok = (c, m) => { if (!c) throw new Error(m); };

// ── Levels exist with the documented shape ────────────────────────────────
ok(ZSResilience.LEVEL_IDS.join(',') === 'off,standard,persistent', `unexpected levels: ${ZSResilience.LEVEL_IDS}`);
for (const id of ZSResilience.LEVEL_IDS) {
  const L = ZSResilience.LEVELS[id];
  ok(typeof L.label === 'string' && L.label.length > 0, `level ${id} has no label`);
  ok(Array.isArray(L.backoffMs), `level ${id} has no backoff array`);
  if (L.maxRetries > 0) ok(L.backoffMs.length > 0, `level ${id} retries but never backs off`);
}
ok(ZSResilience.LEVELS.persistent.maxRetries > ZSResilience.LEVELS.standard.maxRetries, 'persistent must retry more than standard');

// ── Off really is off: a failure ends the run, it is never silently retried ─
for (const kind of ['empty', 'timeout', 'parse_error', 'send_failed']) {
  const d = ZSResilience.decide('off', kind, { attempt: 0, consecutive: 0 });
  ok(d.action === 'stop', `off level retried a ${kind} (${d.action})`);
  ok(d.reason === 'recovery-off', `off level gave an odd reason for ${kind}: ${d.reason}`);
}

// ── Standard recovers from a one-off, then gives up ──────────────────────
const L = ZSResilience.LEVELS.standard;
for (const kind of ['empty', 'timeout']) {
  const first = ZSResilience.decide('standard', kind, { attempt: 0, consecutive: 0 });
  ok(first.action === 'retry', `standard did not retry the first ${kind}`);
  ok(first.delayMs === L.backoffMs[0], `first ${kind} retry used the wrong backoff (${first.delayMs})`);
  ok(first.feedback.includes('Nothing from it was executed'), `${kind} feedback must say nothing ran`);
  ok(first.feedback.includes('exactly ONE plain-text JSON object'), `${kind} feedback must restate the JSON contract`);
  const last = ZSResilience.decide('standard', kind, { attempt: L.maxRetries - 1, consecutive: 0 });
  ok(last.action === 'retry', `standard gave up one retry early on ${kind}`);
  const spent = ZSResilience.decide('standard', kind, { attempt: L.maxRetries, consecutive: 0 });
  ok(spent.action === 'stop', `standard kept retrying ${kind} past its budget`);
  ok(spent.reason.endsWith('budget-spent'), `unexpected give-up reason: ${spent.reason}`);
  ok(spent.attemptsLeft === 0, 'spent budget must report zero attempts left');
}
// The backoff escalates and then repeats its last value (never grows forever).
const b0 = ZSResilience.decide('standard', 'empty', { attempt: 0, consecutive: 0 }).delayMs;
const b1 = ZSResilience.decide('standard', 'empty', { attempt: 1, consecutive: 0 }).delayMs;
ok(b1 > b0, 'backoff must escalate');
const bBig = ZSResilience.decide('persistent', 'empty', { attempt: ZSResilience.LEVELS.persistent.maxRetries - 1, consecutive: 0 });
ok(bBig.action === 'retry' && bBig.delayMs === ZSResilience.LEVELS.persistent.backoffMs.slice(-1)[0],
  'the last in-budget retry must use the final backoff value');
// The backoff ladder must never go backwards.
const ladder = ZSResilience.LEVELS.persistent.backoffMs;
for (let i = 1; i < ladder.length; i++) ok(ladder[i] >= ladder[i - 1], 'backoff ladder must be non-decreasing');

// ── A run of failures stops even while retries remain (no infinite loop) ─
const capped = ZSResilience.decide('persistent', 'empty', { attempt: 0, consecutive: ZSResilience.LEVELS.persistent.maxConsecutive });
ok(capped.action === 'stop', 'a long failure streak must stop the loop');
ok(capped.reason === 'too-many-consecutive-failures', `unexpected streak reason: ${capped.reason}`);
const nearCap = ZSResilience.decide('persistent', 'empty', { attempt: 0, consecutive: ZSResilience.LEVELS.persistent.maxConsecutive - 1 });
ok(nearCap.action === 'retry', 'the last allowed failure in a streak must still retry');

// ── A transient SITE outage ("busy / please try again") gets ONE retry ────
// DeepSeek at peak hours is the real case. It must be recoverable, but bounded
// to a single attempt so a model answer that merely says "try again" to the user
// can never loop.
{
  const off1 = ZSResilience.decide('off', 'busy', { attempt: 0, consecutive: 0 });
  ok(off1.action === 'stop', 'off level must not retry a busy notice');
  for (const level of ['standard', 'persistent']) {
    const first = ZSResilience.decide(level, 'busy', { attempt: 0, consecutive: 0 });
    ok(first.action === 'retry', `${level} must retry a busy notice once`);
    ok(first.reason === 'busy', `unexpected busy reason at ${level}: ${first.reason}`);
    ok(first.feedback.includes('site itself reported'), 'the busy nudge must say the SITE reported it');
    ok(first.feedback.includes('NOTHING from it was executed'), 'the busy nudge must say nothing ran');
    ok(first.feedback.includes('exactly ONE plain-text JSON object'), 'the busy nudge must restate the JSON contract');
    ok(!first.feedback.includes('apologise') === false, 'the busy nudge must tell the model not to apologise');
    const second = ZSResilience.decide(level, 'busy', { attempt: 1, consecutive: 0 });
    ok(second.action === 'stop', `${level} must stop after a single busy retry`);
    ok(second.reason === 'busy-retry-spent', `unexpected second busy reason: ${second.reason}`);
    ok(ZSResilience.LEVELS[level].maxRetries >= 1, 'a level that retries busy must have a budget');
  }
  const streak = ZSResilience.decide('persistent', 'busy', { attempt: 0, consecutive: ZSResilience.LEVELS.persistent.maxConsecutive });
  ok(streak.action === 'stop', 'a busy notice inside a long failure streak must stop');
}

// ── parse_error carries the caller's exact feedback and detected name ────
const pe = ZSResilience.decide('standard', 'parse_error', { attempt: 0, consecutive: 0, name: 'multi_edit', feedback: 'ERROR: fix your JSON' });
ok(pe.action === 'retry' && pe.feedback === 'ERROR: fix your JSON', 'parse_error must pass the caller feedback through unchanged');
const peDefault = ZSResilience.decide('standard', 'parse_error', { attempt: 0, consecutive: 0, name: 'multi_edit' });
ok(peDefault.feedback.includes('multi_edit'), 'the default parse feedback must name the detected command');
ok(peDefault.feedback.includes('NOT executed'), 'the default parse feedback must be explicit that nothing ran');
const peNoName = ZSResilience.decide('standard', 'parse_error', { attempt: 0, consecutive: 0 });
ok(!peNoName.feedback.includes('undefined'), 'an unknown command name must not leak "undefined"');
ok(!/The command name detected was/.test(peNoName.feedback), 'an unknown command name must omit the name clause entirely');
ok(peNoName.feedback.includes('{"command": "exact_name"'), 'the default parse feedback must still show the required envelope');

// ── send_failed re-sends the SAME text, it never invents a prompt ────────
const sf = ZSResilience.decide('standard', 'send_failed', { attempt: 0, consecutive: 0 });
ok(sf.action === 'resend', `send_failed must resend, got ${sf.action}`);
ok(sf.feedback === '', 'send_failed must not fabricate a model-facing message');

// ── Verification and environment outages PARK, they never retry ──────────
for (const kind of ['verification', 'bridge_offline']) {
  for (const level of ZSResilience.LEVEL_IDS) {
    const d = ZSResilience.decide(level, kind, { attempt: 0, consecutive: 0 });
    ok(d.action === 'wait', `${kind} at level ${level} must wait, got ${d.action}`);
    ok(d.delayMs === 0, `${kind} must not impose its own delay`);
  }
}
ok(ZSResilience.decide('persistent', 'terminal', {}).action === 'stop', 'a terminal condition must stop');
ok(ZSResilience.decide('persistent', 'nonsense', {}).action === 'stop', 'an unknown condition must stop, never loop');

// ── Budgets widen for a persistent run, and never shrink below the base ──
const bs = ZSResilience.budgets('standard'), bp = ZSResilience.budgets('persistent');
for (const k of ['preStartSilentMs', 'warmupMs', 'reasonNoReplyMs', 'openBlockGraceMs', 'verificationWaitMs']) {
  ok(bp[k] >= bs[k], `persistent ${k} must be at least the standard value`);
}
ok(bp.autoContinue === true && bs.autoContinue === false, 'only persistent should auto-continue');
ok(ZSResilience.budgets('persistent').verificationWaitMs >= 300000, 'verification waits must be generous enough for a real user');

// ── Settings: sanitisation, per-provider level, bad input safety ─────────
const s = ZSResilience.sanitize({ level: 'nope', perProvider: { arena: 'persistent', deepseek: 'bogus', 'bad id': 'off' } });
ok(s.level === 'standard', 'an unknown level must fall back to standard');
ok(s.perProvider.arena === 'persistent', 'a valid per-provider level must survive');
ok(!('deepseek' in s.perProvider), 'an invalid per-provider level must be dropped');
ok(!('bad id' in s.perProvider), 'an invalid provider id must be dropped');
ok(ZSResilience.levelFor(s, 'arena') === 'persistent', 'per-provider level must win for that provider');
ok(ZSResilience.levelFor(s, 'gemini') === 'standard', 'other providers must keep the global level');
ok(ZSResilience.levelFor({}, 'anything') === 'standard', 'missing settings must resolve to standard');
ok(ZSResilience.levelFor(null, null) === 'standard', 'null settings must resolve to standard');
ok(typeof ZSResilience.describe(s, 'arena') === 'string' && ZSResilience.describe(s, 'arena').includes('Persistent'), 'describe must name the effective level');

console.log('PASS error recovery policy: off/standard/persistent, escalating backoff, per-failure + consecutive budgets, parse + send + verification branches, widened persistent budgets, sanitised settings');

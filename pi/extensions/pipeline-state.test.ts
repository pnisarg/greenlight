import assert from "node:assert/strict";
import { test } from "node:test";

import {
	applyLines,
	initialState,
	plain,
	renderCard,
	summarize,
} from "./pipeline-state.ts";

/** A real passing backend run, as captured from `greenlight run` with GREENLIGHT_EVENTS. */
const PASSING = [
	{ ts: 1, type: "run_start", branch: "feat/demo", classification: "backend", files: [".greenlight.toml", "calc.py"] },
	{ ts: 2, type: "intent", source: "supplied", text: "Add a mul() helper for arithmetic" },
	{ ts: 3, type: "lint", status: "skip", fixed: false },
	{ ts: 4, type: "review_round", round: 1, max_rounds: 3 },
	{ ts: 5, type: "reviewer", name: "brutal", round: 1, findings: null, blocking: null },
	{ ts: 6, type: "reviewer", name: "brutal", round: 1, findings: 0, blocking: 0 },
	{ ts: 7, type: "reviewer", name: "security", round: 1, findings: null, blocking: null },
	{ ts: 8, type: "reviewer", name: "security", round: 1, findings: 0, blocking: 0 },
	{ ts: 9, type: "verify", target: "backend", status: "skip", evidence: [] },
	{ ts: 10, type: "pr", status: "open", url: "https://github.com/x/y/pull/1" },
	{ ts: 11, type: "run_end", passed: true },
]
	.map((e) => JSON.stringify(e))
	.join("\n");

test("reduces a full passing run to the right snapshot", () => {
	const s = initialState();
	const n = applyLines(s, PASSING);
	assert.equal(n, 11);
	assert.equal(s.passed, true);
	assert.equal(s.branch, "feat/demo");
	assert.equal(s.classification, "backend");
	assert.equal(s.fileCount, 2);
	assert.equal(s.intentSource, "supplied");
	assert.equal(s.stages.intent, "done");
	assert.equal(s.stages.lint, "skip");
	assert.equal(s.stages.review, "done");
	assert.equal(s.stages.verify, "skip");
	assert.equal(s.stages.pr, "done");
	assert.equal(s.review.reviewers.length, 2);
	assert.ok(s.review.reviewers.every((r) => r.status === "done"));
	assert.equal(s.parseErrors, 0);
});

test("two-phase reviewer events flip running -> done", () => {
	const s = initialState();
	applyLines(
		s,
		[
			{ type: "review_round", round: 1, max_rounds: 3 },
			{ type: "reviewer", name: "brutal", round: 1, findings: null, blocking: null },
		]
			.map((e) => JSON.stringify(e))
			.join("\n"),
	);
	assert.equal(s.review.reviewers[0].status, "running");
	assert.equal(s.stages.review, "running");

	applyLines(s, JSON.stringify({ type: "reviewer", name: "brutal", round: 1, findings: 3, blocking: 1 }));
	assert.equal(s.review.reviewers[0].status, "done");
	assert.equal(s.review.reviewers[0].findings, 3);
	assert.equal(s.review.reviewers[0].blocking, 1);
});

test("fix event bumps the counter and re-runs reviewers", () => {
	const s = initialState();
	applyLines(
		s,
		[
			{ type: "review_round", round: 1, max_rounds: 3 },
			{ type: "reviewer", name: "brutal", round: 1, findings: 2, blocking: 1 },
			{ type: "fix", round: 1, findings: 1 },
		]
			.map((e) => JSON.stringify(e))
			.join("\n"),
	);
	assert.equal(s.review.fixes, 1);
	assert.equal(s.review.reviewers[0].status, "running");
});

test("failure marks the blocking gate and leaves later stages pending", () => {
	const s = initialState();
	applyLines(
		s,
		[
			{ type: "run_start", branch: "feat/x", classification: "backend", files: ["a.py"] },
			{ type: "intent", source: "supplied", text: "x" },
			{ type: "lint", status: "pass", fixed: false },
			{ type: "review_round", round: 3, max_rounds: 3 },
			{ type: "reviewer", name: "brutal", round: 3, findings: 1, blocking: 1 },
			{ type: "run_end", passed: false },
		]
			.map((e) => JSON.stringify(e))
			.join("\n"),
	);
	assert.equal(s.passed, false);
	assert.equal(s.stages.lint, "done");
	assert.equal(s.stages.review, "fail");
	assert.equal(s.stages.verify, "pending");
	assert.equal(s.stages.pr, "pending");
});

test("malformed lines are counted, not thrown", () => {
	const s = initialState();
	const n = applyLines(s, 'not json\n{"type":"run_end","passed":true}\n\n{bad');
	assert.equal(n, 1);
	assert.equal(s.parseErrors, 2);
	assert.equal(s.passed, true);
});

test("renderCard emits the agent-handoff bookends", () => {
	const s = initialState();
	applyLines(s, PASSING);
	const lines = renderCard(s, plain);
	assert.match(lines[0], /^greenlight /);
	assert.ok(lines.some((l) => l.includes("intent") && l.includes("supplied by agent")));
	assert.ok(lines.some((l) => l.includes("brutal") && l.includes("blocking")));
	assert.match(lines.at(-1) ?? "", /PASSED — handed back to agent/);
});

test("summarize flags reconstructed intent for the agent", () => {
	const s = initialState();
	applyLines(
		s,
		[
			{ type: "run_start", branch: "b", classification: "backend", files: ["a.py"] },
			{ type: "intent", source: "reconstructed", text: "guessed" },
			{ type: "run_end", passed: true },
		]
			.map((e) => JSON.stringify(e))
			.join("\n"),
	);
	const out = summarize(s);
	assert.match(out, /reconstructed from the diff/);
});

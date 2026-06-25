import assert from "node:assert/strict";
import { test } from "node:test";

import {
	applyLines,
	blockingFindings,
	initialState,
	plain,
	renderCard,
	stageElapsed,
	statusLine,
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

/** A failing run: blocking findings remain after max rounds. */
const FAILING = [
	{ ts: 100, type: "run_start", branch: "feat/bug", classification: "backend", files: ["api.py"] },
	{ ts: 101, type: "intent", source: "supplied", text: "add endpoint" },
	{ ts: 102, type: "lint", status: "pass", fixed: false },
	{ ts: 103, type: "review_round", round: 3, max_rounds: 3 },
	{ ts: 104, type: "reviewer", name: "brutal", round: 3, findings: null, blocking: null },
	{
		ts: 130,
		type: "reviewer",
		name: "brutal",
		round: 3,
		findings: 2,
		blocking: 1,
		items: [
			{ severity: "error", file: "api.py", line: 42, description: "SQL injection in query", blocks: true },
			{ severity: "info", file: "api.py", line: 5, description: "unused import", blocks: false },
		],
	},
	{ ts: 131, type: "run_end", passed: false },
]
	.map((e) => JSON.stringify(e))
	.join("\n");

test("parses finding items onto the reviewer", () => {
	const s = initialState();
	applyLines(s, FAILING);
	const brutal = s.review.reviewers.find((r) => r.name === "brutal");
	assert.ok(brutal);
	assert.equal(brutal.items.length, 2);
	assert.equal(brutal.items[0].file, "api.py");
	assert.equal(brutal.items[0].line, 42);
	assert.equal(brutal.items[0].blocks, true);
	assert.equal(brutal.items[1].blocks, false);
});

test("blockingFindings returns only blocking items across reviewers", () => {
	const s = initialState();
	applyLines(s, FAILING);
	const blk = blockingFindings(s);
	assert.equal(blk.length, 1);
	assert.match(blk[0].description, /SQL injection/);
});

test("failure card names the blocked gate and surfaces blocking findings", () => {
	const s = initialState();
	applyLines(s, FAILING);
	assert.equal(s.failedStage, "review");
	const lines = renderCard(s, plain);
	assert.ok(lines.some((l) => /blocked at review/.test(l)));
	assert.ok(lines.some((l) => /SQL injection/.test(l)));
});

test("expanded mode lists blocking findings under the reviewer", () => {
	const s = initialState();
	applyLines(s, FAILING);
	const lines = renderCard(s, plain, { expanded: true });
	// Indented finding line under the reviewer (8 spaces) appears before the verdict.
	assert.ok(lines.some((l) => l.startsWith("        ") && /SQL injection/.test(l)));
});

test("stageElapsed bounds a stage by the next stage's start", () => {
	const s = initialState();
	applyLines(s, FAILING);
	// review started at 103, run_end (no later stage) at 131 -> ~28s.
	const secs = stageElapsed(s, "review", s.lastTs);
	assert.equal(secs, 28);
	// intent started 100, lint at 102 -> 2s.
	assert.equal(stageElapsed(s, "intent", s.lastTs), 2);
});

test("renderCard shows elapsed and a custom spinner on the running stage", () => {
	const s = initialState();
	applyLines(
		s,
		[
			{ ts: 1, type: "run_start", branch: "b", classification: "backend", files: ["a.py"] },
			{ ts: 1, type: "intent", source: "supplied", text: "x" },
			{ ts: 2, type: "lint", status: "pass", fixed: false },
			{ ts: 3, type: "review_round", round: 1, max_rounds: 3 },
			{ ts: 3, type: "reviewer", name: "brutal", round: 1, findings: null, blocking: null },
		]
			.map((e) => JSON.stringify(e))
			.join("\n"),
	);
	const lines = renderCard(s, plain, { now: 18, spinner: "◐" });
	const review = lines.find((l) => l.includes("review"));
	assert.ok(review);
	assert.match(review, /◐/); // spinner on the running stage
	assert.match(review, /\(15s\)/); // 18 - 3 = 15s elapsed
});

test("statusLine reflects running stage, pass, and fail", () => {
	const running = initialState();
	applyLines(
		running,
		[
			{ ts: 1, type: "run_start", branch: "b", classification: "backend", files: ["a.py"] },
			{ ts: 1, type: "intent", source: "supplied", text: "x" },
			{ ts: 2, type: "review_round", round: 2, max_rounds: 3 },
		]
			.map((e) => JSON.stringify(e))
			.join("\n"),
	);
	assert.match(statusLine(running), /review 2\/3/);

	const passed = initialState();
	applyLines(passed, PASSING);
	assert.match(statusLine(passed), /passed/);

	const failed = initialState();
	applyLines(failed, FAILING);
	assert.match(statusLine(failed), /failed at review/);
});

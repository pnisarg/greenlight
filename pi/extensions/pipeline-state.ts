/**
 * Pure reducer + renderer for the greenlight pipeline event stream.
 *
 * greenlight (the Python gate) emits one JSON object per line to the file named
 * by GREENLIGHT_EVENTS. This module turns that stream into a snapshot of the
 * pipeline and renders it as the live tool-call card. It is deliberately free of
 * any pi/TUI imports so it can be unit-tested with plain node and reused for any
 * surface (card, widget, web).
 *
 * The handoff this visualizes: the coding agent authors the intent and hands off
 * to greenlight; the autonomous pipeline (lint -> review loop -> verify -> PR)
 * runs in a throwaway worktree; the result is handed back. The card's first row
 * is "intent (from agent)" and the last is the returned verdict, so the boundary
 * between agent and gate stays legible.
 */

export type StageStatus = "pending" | "running" | "done" | "fail" | "skip";

export type StageName = "intent" | "lint" | "review" | "verify" | "pr";

export const STAGE_ORDER: StageName[] = ["intent", "lint", "review", "verify", "pr"];

export interface ReviewerState {
	name: string;
	status: StageStatus;
	findings: number | null;
	blocking: number | null;
}

export interface PipelineState {
	branch: string;
	classification: string;
	fileCount: number;
	intentSource: "supplied" | "reconstructed" | null;
	intentText: string;
	stages: Record<StageName, StageStatus>;
	review: {
		round: number;
		maxRounds: number;
		fixes: number;
		reviewers: ReviewerState[];
	};
	verify: { target: string; status: StageStatus }[];
	pr: { status: string; url: string };
	passed: boolean | null;
	/** Any line that failed to parse, kept for debugging; not rendered. */
	parseErrors: number;
}

export interface GreenlightEvent {
	type: string;
	[key: string]: unknown;
}

export function initialState(): PipelineState {
	return {
		branch: "",
		classification: "",
		fileCount: 0,
		intentSource: null,
		intentText: "",
		stages: { intent: "pending", lint: "pending", review: "pending", verify: "pending", pr: "pending" },
		review: { round: 0, maxRounds: 0, fixes: 0, reviewers: [] },
		verify: [],
		pr: { status: "", url: "" },
		passed: null,
		parseErrors: 0,
	};
}

function statusFor(s: string): StageStatus {
	if (s === "pass") return "done";
	if (s === "fail") return "fail";
	if (s === "skip") return "skip";
	return "pending";
}

/** Apply one event to the state, mutating and returning it. Order-tolerant. */
export function reduce(state: PipelineState, ev: GreenlightEvent): PipelineState {
	switch (ev.type) {
		case "run_start": {
			state.branch = String(ev.branch ?? "");
			state.classification = String(ev.classification ?? "");
			state.fileCount = Array.isArray(ev.files) ? ev.files.length : 0;
			state.stages.intent = "running";
			break;
		}
		case "intent": {
			state.intentSource = ev.source === "supplied" ? "supplied" : "reconstructed";
			state.intentText = String(ev.text ?? "");
			state.stages.intent = "done";
			break;
		}
		case "lint": {
			state.stages.lint = statusFor(String(ev.status));
			break;
		}
		case "review_round": {
			state.stages.review = "running";
			state.review.round = Number(ev.round ?? 0);
			state.review.maxRounds = Number(ev.max_rounds ?? 0);
			break;
		}
		case "reviewer": {
			const name = String(ev.name ?? "");
			const findings = ev.findings == null ? null : Number(ev.findings);
			const blocking = ev.blocking == null ? null : Number(ev.blocking);
			let r = state.review.reviewers.find((x) => x.name === name);
			if (!r) {
				r = { name, status: "running", findings: null, blocking: null };
				state.review.reviewers.push(r);
			}
			// findings == null marks "started"; a later event with counts marks "done".
			if (findings == null) {
				r.status = "running";
			} else {
				r.status = "done";
				r.findings = findings;
				r.blocking = blocking;
			}
			break;
		}
		case "fix": {
			state.review.fixes += 1;
			// A new fix round is coming; the reviewers will re-run.
			for (const r of state.review.reviewers) r.status = "running";
			break;
		}
		case "verify": {
			state.stages.review = settleReview(state);
			const target = String(ev.target ?? "");
			const status = statusFor(String(ev.status));
			const existing = state.verify.find((v) => v.target === target);
			if (existing) existing.status = status;
			else state.verify.push({ target, status });
			state.stages.verify = aggregateVerify(state);
			break;
		}
		case "pr": {
			state.stages.review = settleReview(state);
			state.pr.status = String(ev.status ?? "");
			state.pr.url = String(ev.url ?? "");
			state.stages.pr =
				state.pr.status === "open" || state.pr.status === "exists"
					? "done"
					: state.pr.status === "skip"
						? "skip"
						: state.pr.status === "fail"
							? "fail"
							: "pending";
			break;
		}
		case "run_end": {
			state.passed = Boolean(ev.passed);
			finalize(state);
			break;
		}
		default:
			break;
	}
	return state;
}

/** Once we leave the review loop cleanly, a still-running review stage is done. */
function settleReview(state: PipelineState): StageStatus {
	return state.stages.review === "running" ? "done" : state.stages.review;
}

function aggregateVerify(state: PipelineState): StageStatus {
	if (state.verify.some((v) => v.status === "fail")) return "fail";
	if (state.verify.length > 0 && state.verify.every((v) => v.status === "skip")) return "skip";
	if (state.verify.length > 0) return "done";
	return "running";
}

function finalize(state: PipelineState): void {
	if (state.passed) {
		// Everything up to PR cleared; promote any lingering running/pending
		// gates (PR keeps its explicit status — a failed PR doesn't fail the gate).
		for (const s of ["intent", "lint", "review", "verify"] as StageName[]) {
			if (state.stages[s] === "running" || state.stages[s] === "pending") state.stages[s] = "done";
		}
		return;
	}
	// Failed: the first gate still "running" is the one that blocked. Leave the
	// stages after it as pending so the card shows where the pipeline stopped.
	for (const s of ["intent", "lint", "review", "verify", "pr"] as StageName[]) {
		if (state.stages[s] === "running") {
			state.stages[s] = "fail";
			break;
		}
	}
}

/** Feed a chunk of JSONL, returning the number of events applied. */
export function applyLines(state: PipelineState, text: string): number {
	let applied = 0;
	for (const line of text.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed) continue;
		try {
			reduce(state, JSON.parse(trimmed) as GreenlightEvent);
			applied += 1;
		} catch {
			state.parseErrors += 1;
		}
	}
	return applied;
}

const GLYPH: Record<StageStatus, string> = {
	pending: "·",
	running: "⟳",
	done: "✓",
	fail: "✗",
	skip: "⊘",
};

/** Color category per status, mapped to theme colors by the caller. */
export type ColorName = "ok" | "fail" | "running" | "dim" | "muted" | "accent";

const COLOR: Record<StageStatus, ColorName> = {
	pending: "dim",
	running: "running",
	done: "ok",
	fail: "fail",
	skip: "dim",
};

export interface Painter {
	ok: (s: string) => string;
	fail: (s: string) => string;
	running: (s: string) => string;
	dim: (s: string) => string;
	muted: (s: string) => string;
	accent: (s: string) => string;
	bold: (s: string) => string;
}

/** Identity painter — useful for tests and non-TUI output. */
export const plain: Painter = {
	ok: (s) => s,
	fail: (s) => s,
	running: (s) => s,
	dim: (s) => s,
	muted: (s) => s,
	accent: (s) => s,
	bold: (s) => s,
};

function paint(p: Painter, name: ColorName, s: string): string {
	return p[name](s);
}

const STAGE_LABEL: Record<StageName, string> = {
	intent: "intent",
	lint: "lint",
	review: "review",
	verify: "verify",
	pr: "PR",
};

function stageDetail(state: PipelineState, stage: StageName): string {
	switch (stage) {
		case "intent":
			return state.intentSource ? `${state.intentSource} by agent` : "";
		case "review": {
			if (state.review.maxRounds) {
				const fixes = state.review.fixes ? `, ${state.review.fixes} fix${state.review.fixes > 1 ? "es" : ""}` : "";
				return `round ${state.review.round}/${state.review.maxRounds}${fixes}`;
			}
			return "";
		}
		case "verify":
			return state.verify.map((v) => `${v.target} ${v.status}`).join(", ");
		case "pr":
			return state.pr.url && state.pr.status !== "fail" ? state.pr.url : state.pr.status;
		default:
			return "";
	}
}

/**
 * Render the pipeline as boxed card lines. `p` maps semantic colors to the
 * active theme (or identity for plain text). Returns an array of lines.
 */
export function renderCard(state: PipelineState, p: Painter = plain): string[] {
	const rows: string[] = [];
	const header =
		state.branch
			? `${state.branch}  ·  ${state.classification || "?"}  ·  ${state.fileCount} file${state.fileCount === 1 ? "" : "s"}`
			: "starting…";
	rows.push(p.bold("greenlight ") + p.muted(header));

	for (const stage of STAGE_ORDER) {
		const st = state.stages[stage];
		const glyph = paint(p, COLOR[st], GLYPH[st]);
		const label = STAGE_LABEL[stage].padEnd(7);
		let line = `  ${glyph} ${label}`;
		const detail = stageDetail(state, stage);
		if (detail) line += " " + p.dim(detail);
		rows.push(line);
		// Nested reviewer lines under the review stage.
		if (stage === "review" && state.review.reviewers.length) {
			for (const r of state.review.reviewers) {
				const g = paint(p, COLOR[r.status], GLYPH[r.status]);
				let sub = `      ${g} ${r.name.padEnd(10)}`;
				if (r.status === "done" && r.findings != null) {
					sub += " " + p.dim(`${r.findings} finding${r.findings === 1 ? "" : "s"}, ${r.blocking} blocking`);
				} else if (r.status === "running") {
					sub += " " + p.dim("running…");
				}
				rows.push(sub);
			}
		}
	}

	if (state.passed != null) {
		const verdict = state.passed ? p.ok("● PASSED — handed back to agent") : p.fail("● FAILED — nothing forwarded");
		rows.push(verdict);
	}
	return rows;
}

/** One-line plain-text verdict + summary for the LLM (the tool's text content). */
export function summarize(state: PipelineState): string {
	const lines: string[] = [];
	const head =
		state.passed == null
			? "greenlight: (incomplete)"
			: state.passed
				? "greenlight: PASSED"
				: "greenlight: FAILED";
	const ctx = state.branch ? ` — branch ${state.branch}, ${state.classification}, ${state.fileCount} files` : "";
	lines.push(head + ctx);

	if (state.intentSource) lines.push(`intent: ${state.intentSource}`);
	lines.push(`lint: ${state.stages.lint}`);

	if (state.review.maxRounds) {
		const rv = state.review.reviewers
			.map((r) => `${r.name} ${r.findings ?? "?"}/${r.blocking ?? "?"} blk`)
			.join(", ");
		const fixes = state.review.fixes ? `, ${state.review.fixes} fix round(s)` : "";
		lines.push(`review: ${state.stages.review} after round ${state.review.round}/${state.review.maxRounds}${fixes}${rv ? ` (${rv})` : ""}`);
	} else {
		lines.push(`review: ${state.stages.review}`);
	}

	if (state.verify.length) {
		lines.push(`verify: ${state.verify.map((v) => `${v.target} ${v.status}`).join(", ")}`);
	} else {
		lines.push(`verify: ${state.stages.verify}`);
	}

	if (state.pr.status) {
		lines.push(`pr: ${state.pr.status}${state.pr.url && state.pr.status !== "fail" ? ` ${state.pr.url}` : ""}`);
	}
	if (state.intentSource === "reconstructed") {
		lines.push("note: intent was reconstructed from the diff (none supplied) — review ran on a weaker signal.");
	}
	return lines.join("\n");
}
